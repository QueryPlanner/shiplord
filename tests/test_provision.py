from __future__ import annotations

import base64
import io
import json
import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path

from shiplord.admin import parse_request, run, serve
from shiplord.framing import read_frame, write_frame
from shiplord.layout import Layout
from shiplord.protocol import ProtocolError
from shiplord.provision import ProvisionError, deprovision, manage_environment, manage_key, manage_registry, provision
from shiplord.release_state import HostLock, ReleaseState
from shiplord.system import CommandResult


def release(app: str = "orders", commit: str = "a", image: str = "b") -> dict:
    return {"commit": commit * 40, "sequence": 1000,
            "artifact": {"kind": "docker", "image": f"ghcr.io/example/{app}@sha256:" + image * 64},
            "configuration": {"schema_version": 1, "name": app, "server": "production", "template": "google-adk",
                              "runtime": "docker", "http": {"api_domain": f"{app}.example.com", "frontend_origins": [],
                              "container_port": 8080, "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
                              "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}}


def ed25519(seed: int) -> str:
    def field(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value
    blob = field(b"ssh-ed25519") + field(bytes([seed]) * 32)
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + f" comment{seed}"


class FakeSystem:
    """Emulates the account database and visudo; records every command."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.visudo_code = 0
        self.next_uid = 900
        self.fail = set()
        self.busy_userdel = 0

    def run(self, argv, *, input=None, timeout=60):
        argv = list(argv)
        self.calls.append(argv)
        tool = argv[0].rsplit("/", 1)[-1]
        if tool in self.fail:
            return CommandResult(1, "failed")
        if tool == "getent":
            user = self.users.get(argv[2])
            if user is None:
                return CommandResult(2, "")
            return CommandResult(0, f"{argv[2]}:x:{user['uid']}:{user['uid']}:{user['comment']}:{user['home']}:{user['shell']}\n")
        if tool == "useradd":
            options = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 2) if argv[i].startswith("--") and argv[i + 1][:1] != "-"}
            self.users[argv[-1]] = {"uid": self.next_uid, "comment": options["--comment"], "home": options["--home-dir"],
                                    "shell": options["--shell"], "locked": False, "flags": argv[1:-1]}
            self.next_uid += 1
            return CommandResult(0, "")
        if tool == "usermod":
            self.users[argv[-1]]["password"] = argv[argv.index("--password") + 1]
            return CommandResult(0, "")
        if tool == "userdel":
            if self.busy_userdel:  # a lingering session: userdel reports "currently logged in" until it ends
                self.busy_userdel -= 1
                return CommandResult(8, "userdel: user is currently used by process 1234")
            return CommandResult(0, "") if self.users.pop(argv[-1], None) else CommandResult(6, "")
        if tool == "loginctl":
            return CommandResult(0, "")
        if tool == "systemctl":
            return CommandResult(0, "")
        if tool == "visudo":
            self.validated = Path(argv[-1]).read_text()
            return CommandResult(self.visudo_code, "")
        raise AssertionError(f"unexpected command {argv}")


class ProvisionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        for directory, mode in ((self.layout.registrations, 0o700), (self.layout.environments, 0o700),
                                (self.layout.authorized_keys, 0o755), (self.layout.var, 0o755),
                                (self.layout.app_state, 0o700), (self.layout.sudoers_dir, 0o755),
                                (self.layout.data, 0o755), (self.layout.releases, 0o755),
                                (self.layout.registry_logins, 0o700)):
            directory.mkdir(parents=True)
            directory.chmod(mode)
        self.fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.lock = HostLock(self.fd)
        self.system = FakeSystem()

    def provision(self, app="orders", **changes):
        arguments = {"application_id": app, "runtime": "docker", "api_domain": f"{app}.example.com",
                     "image_repository": f"ghcr.io/example/{app}", "public_key": ed25519(1), "key_label": "laptop"}
        arguments.update(changes)
        with self.lock.acquire():
            return provision(self.layout, self.system, self.lock, **arguments)

    def snapshot(self):
        return {str(p.relative_to(self.layout.prefix)): p.read_bytes()
                for p in self.layout.prefix.rglob("*") if p.is_file() and p.name != "host.lock"}


class ProvisionTests(ProvisionTestCase):
    def test_first_provision_creates_every_resource(self):
        result = self.provision()
        self.assertEqual(result["deployment_user"], "ship-orders")
        self.assertEqual(result["host_port"], 20000)
        self.assertTrue(all(result["created"].values()), result["created"])
        user = self.system.users["ship-orders"]
        self.assertEqual((user["comment"], user["home"], user["shell"], user["password"]),
                         ("shiplord-deploy-orders", "/", "/bin/sh", "*"))
        for flag in ("--system", "--user-group", "--no-create-home"):
            self.assertIn(flag, user["flags"])
        layout = self.layout
        self.assertEqual(stat.S_IMODE((layout.app_state / "orders").stat().st_mode), 0o700)
        self.assertEqual(json.loads((layout.app_state / "orders" / "state.json").read_text())["generation"], 0)
        self.assertEqual(stat.S_IMODE((layout.environments / "orders").stat().st_mode), 0o700)
        keys = (layout.authorized_keys / "ship-orders").read_text()
        self.assertIn('command="/usr/local/libexec/shiplord-dispatch orders",restrict,no-pty ssh-ed25519 ', keys)
        self.assertNotIn("comment1", keys)
        sudoers = layout.sudoers_dir / "shiplord-orders"
        self.assertEqual(sudoers.read_text(), f'ship-orders ALL=(root) NOPASSWD: NOSETENV: {layout.host} ""\n')
        self.assertEqual(stat.S_IMODE(sudoers.stat().st_mode), 0o440)
        self.assertEqual(self.system.validated, sudoers.read_text())
        record = json.loads((layout.registrations / "orders.json").read_text())
        self.assertEqual((record["deployment_uid"], record["deployment_user"], record["host_port"]),
                         (user["uid"], "ship-orders", 20000))
        self.assertEqual([p.name for p in layout.sudoers_dir.iterdir()], ["shiplord-orders"])

    def test_repeating_provision_changes_nothing_and_reuses_the_account(self):
        first = self.provision()
        before, calls = self.snapshot(), len(self.system.calls)
        second = self.provision()
        self.assertFalse(any(second["created"].values()), second["created"])
        self.assertEqual((second["host_port"], second["deployment_user"]), (first["host_port"], first["deployment_user"]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([c[0].rsplit("/", 1)[-1] for c in self.system.calls[calls:]], ["getent"])

    def test_second_application_is_isolated(self):
        self.provision("orders")
        other = self.provision("billing", public_key=ed25519(2))
        self.assertEqual((other["host_port"], other["deployment_user"]), (20001, "ship-billing"))
        self.assertNotEqual(self.system.users["ship-orders"]["uid"], self.system.users["ship-billing"]["uid"])
        self.assertNotIn(ed25519(2).split()[1], (self.layout.authorized_keys / "ship-orders").read_text())

    def test_a_foreign_account_with_the_same_name_is_never_adopted(self):
        self.system.users["ship-orders"] = {"uid": 777, "comment": "someone else", "home": "/home/x", "shell": "/bin/bash"}
        before = self.snapshot()
        with self.assertRaisesRegex(ProvisionError, "not a Shiplord"):
            self.provision()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.system.users["ship-orders"]["uid"], 777)

    def test_rejected_sudo_policy_leaves_the_application_inert_and_a_retry_completes(self):
        self.system.visudo_code = 1
        with self.assertRaisesRegex(ProvisionError, "sudo rejected"):
            self.provision()
        self.assertFalse((self.layout.registrations / "orders.json").exists())
        self.assertEqual(list(self.layout.sudoers_dir.iterdir()), [])
        self.system.visudo_code = 0
        result = self.provision()
        self.assertFalse(result["created"]["account"])
        self.assertTrue(result["created"]["sudo_policy"] and result["created"]["registration"])

    def test_registration_conflict_leaves_no_registration(self):
        self.provision("orders")
        with self.assertRaisesRegex(ProvisionError, "registration failed"):
            self.provision("billing", api_domain="orders.example.com", public_key=ed25519(2))
        self.assertFalse((self.layout.registrations / "billing.json").exists())
        self.assertTrue(self.provision("billing", public_key=ed25519(2))["created"]["registration"])

    def test_changing_registered_identity_is_refused(self):
        self.provision()
        for change in ({"api_domain": "other.example.com"}, {"runtime": "native", "image_repository": None},
                       {"image_repository": "ghcr.io/example/other"}):
            with self.subTest(change=change), self.assertRaisesRegex(ProvisionError, "differs"):
                self.provision(**change)

    def test_invalid_inputs_are_rejected_before_creating_accounts(self):
        before_calls = len(self.system.calls)
        bad = [{"public_key": 'command="x" ' + ed25519(1)}, {"public_key": "ssh-ed25519 AAAA"}, {"key_label": "A B"},
               {"runtime": "vm"}, {"api_domain": "BAD"}, {"image_repository": None}]
        for change in bad:
            with self.subTest(change=str(change)[:40]), self.assertRaises(Exception):
                self.provision(**change)
        with self.assertRaises(ValueError):
            self.provision("../x")
        self.assertEqual([c for c in self.system.calls[before_calls:] if "useradd" in c[0]], [])
        self.assertEqual(self.system.users, {})

    def test_useradd_failure_is_reported_without_further_steps(self):
        self.system.fail = {"useradd"}
        with self.assertRaisesRegex(ProvisionError, "useradd failed"):
            self.provision()
        self.assertEqual(list(self.layout.app_state.iterdir()), [])

    def test_requires_the_host_lock(self):
        with self.assertRaisesRegex(ProvisionError, "lock"):
            provision(self.layout, self.system, self.lock, application_id="orders", runtime="docker",
                      api_domain="orders.example.com", image_repository="ghcr.io/example/orders",
                      public_key=ed25519(1), key_label="laptop")
        self.assertEqual(self.system.calls, [])

    def test_unbootstrapped_host_is_reported(self):
        import shutil
        shutil.rmtree(self.layout.registrations)
        with self.assertRaisesRegex(ProvisionError, "bootstrapped"):
            self.provision()


class NativeProvisionTests(ProvisionTestCase):
    def provision_native(self, app="orders", **changes):
        return self.provision(app, runtime="native", image_repository=None, **changes)

    def test_native_provisioning_adds_an_unprivileged_runtime_account_and_private_data(self):
        result = self.provision_native()
        self.assertTrue(result["created"]["runtime_account"] and result["created"]["data_directory"])
        runtime = self.system.users["app-orders"]
        self.assertEqual((runtime["comment"], runtime["home"], runtime["shell"], runtime["password"]),
                         ("shiplord-runtime-orders", "/nonexistent", "/usr/sbin/nologin", "*"))
        self.assertNotEqual(runtime["uid"], self.system.users["ship-orders"]["uid"])  # separate from the deploy login
        self.assertEqual(stat.S_IMODE((self.layout.data / "orders").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.layout.releases / "orders").stat().st_mode), 0o755)
        self.assertEqual(json.loads((self.layout.registrations / "orders.json").read_text())["runtime"], "native")

    def test_docker_provisioning_creates_no_runtime_account(self):
        self.provision()
        self.assertEqual(set(self.system.users), {"ship-orders"})
        self.assertFalse((self.layout.data / "orders").exists())

    def test_repeating_native_provisioning_changes_nothing(self):
        self.provision_native()
        again = self.provision_native()
        self.assertFalse(any(again["created"].values()), again["created"])

    def test_a_foreign_runtime_account_is_never_adopted(self):
        self.system.users["app-orders"] = {"uid": 800, "comment": "other", "home": "/home/x", "shell": "/bin/bash"}
        with self.assertRaisesRegex(ProvisionError, "not a Shiplord"):
            self.provision_native()
        self.assertFalse((self.layout.registrations / "orders.json").exists())

    def test_deprovision_removes_binaries_and_the_unit_but_retains_data_and_says_so(self):
        self.provision_native()
        (self.layout.releases / "orders" / "abc").mkdir()
        (self.layout.data / "orders" / "precious.db").write_text("data")
        unit = self.layout.systemd_units / "shiplord-orders.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\n")
        with self.lock.acquire():
            result = deprovision(self.layout, self.system, self.lock, "orders")
        self.assertTrue(result["removed"]["release_binaries"] and result["removed"]["service_unit"])
        self.assertFalse(unit.exists())
        self.assertEqual((self.layout.data / "orders" / "precious.db").read_text(), "data")
        self.assertEqual(len(result["retained"]), 1)
        self.assertIn("app-orders", self.system.users)
        self.assertTrue(any(c[1:3] == ["disable", "--now"] for c in self.system.calls if c[0].endswith("systemctl")))


class AccountRemovalTests(ProvisionTestCase):
    def test_a_lingering_session_is_ended_and_removal_retried(self):
        self.provision()
        self.system.busy_userdel = 2
        slept = []
        with self.lock.acquire():
            result = deprovision(self.layout, self.system, self.lock, "orders", sleep=slept.append)
        self.assertTrue(result["removed"]["account"])
        tools = [c[0].rsplit("/", 1)[-1] for c in self.system.calls]
        self.assertEqual(tools.count("loginctl"), 1)
        self.assertLess(tools.index("loginctl"), len(tools) - 1 - tools[::-1].index("userdel"))
        self.assertIn(["/usr/bin/loginctl", "terminate-user", "ship-orders"], self.system.calls)
        self.assertEqual(len(slept), 2)

    def test_a_session_that_never_ends_fails_clearly_after_bounded_retries(self):
        self.provision()
        self.system.busy_userdel = 1000
        slept = []
        with self.lock.acquire(), self.assertRaisesRegex(ProvisionError, "end its sessions"):
            deprovision(self.layout, self.system, self.lock, "orders", sleep=slept.append)
        self.assertEqual(len(slept), 10)


class KeyManagementTests(ProvisionTestCase):
    def test_add_list_and_remove_keys(self):
        self.provision()
        self.assertEqual(manage_key(self.layout, "orders", "add", label="ci", public_key=ed25519(2)), {"changed": True})
        self.assertEqual(manage_key(self.layout, "orders", "add", label="ci", public_key=ed25519(2)), {"changed": False})
        listing = manage_key(self.layout, "orders", "list")["keys"]
        self.assertEqual([k["label"] for k in listing], ["laptop", "ci"])
        self.assertEqual(manage_key(self.layout, "orders", "remove", label="laptop"), {"changed": True})
        self.assertEqual(manage_key(self.layout, "orders", "remove", label="laptop"), {"changed": False})

    def test_the_last_key_cannot_be_removed(self):
        self.provision()
        with self.assertRaisesRegex(ProvisionError, "last key"):
            manage_key(self.layout, "orders", "remove", label="laptop")
        self.assertEqual(len(manage_key(self.layout, "orders", "list")["keys"]), 1)

    def test_invalid_operations_are_refused(self):
        self.provision()
        for operation, kwargs in (("rotate", {"label": "x"}), ("add", {"label": "x", "public_key": "junk"}),
                                  ("add", {"label": "Bad Label", "public_key": ed25519(3)})):
            with self.subTest(operation=operation), self.assertRaises(ProvisionError):
                manage_key(self.layout, "orders", operation, **kwargs)


class EnvironmentTests(ProvisionTestCase):
    SECRET = "postgres://user:s3cr3t-value@db/app"

    def path(self):
        return self.layout.environments / "orders" / "app.env"

    def test_set_writes_a_private_sorted_file_and_returns_only_key_names(self):
        self.provision()
        result = manage_environment(self.layout, "orders", "set", text=f"# note\nZ_KEY=1\nDATABASE_URL={self.SECRET}\n")
        self.assertEqual(result, {"keys": ["DATABASE_URL", "Z_KEY"], "changed": True})
        self.assertNotIn("s3cr3t", str(result))
        self.assertEqual(self.path().read_text(), f"DATABASE_URL={self.SECRET}\nZ_KEY=1\n")
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
        self.assertEqual(manage_environment(self.layout, "orders", "keys"), {"keys": ["DATABASE_URL", "Z_KEY"]})

    def test_setting_the_same_values_again_changes_nothing(self):
        self.provision()
        manage_environment(self.layout, "orders", "set", text="A=1\n")
        before = self.path().stat().st_ino
        self.assertFalse(manage_environment(self.layout, "orders", "set", text="A=1\n")["changed"])
        self.assertEqual(self.path().stat().st_ino, before)

    def test_clear_removes_the_file_and_is_idempotent(self):
        self.provision()
        manage_environment(self.layout, "orders", "set", text="A=1\n")
        self.assertTrue(manage_environment(self.layout, "orders", "clear")["changed"])
        self.assertFalse(self.path().exists())
        self.assertFalse(manage_environment(self.layout, "orders", "clear")["changed"])
        self.assertEqual(manage_environment(self.layout, "orders", "keys"), {"keys": []})

    def test_invalid_input_never_echoes_values_and_leaves_the_file_alone(self):
        self.provision()
        manage_environment(self.layout, "orders", "set", text="KEEP=1\n")
        for text in (f"PORT={self.SECRET}\n", f"BAD LINE {self.SECRET}\n", f"A={self.SECRET}\nA=2\n", None):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ProvisionError) as caught:
                manage_environment(self.layout, "orders", "set", text=text)
            self.assertNotIn("s3cr3t", str(caught.exception))
        with self.assertRaises(ProvisionError):
            manage_environment(self.layout, "orders", "merge", text="A=1\n")
        self.assertEqual(self.path().read_text(), "KEEP=1\n")
        self.assertEqual([p.name for p in self.path().parent.iterdir()], ["app.env"])

    def test_an_unprovisioned_application_is_refused(self):
        with self.assertRaisesRegex(ProvisionError, "not provisioned"):
            manage_environment(self.layout, "orders", "keys")

    def test_through_the_admin_entry_point_the_secret_travels_only_in_the_frame(self):
        self.provision()
        request = envelope("environment", arguments={"operation": "set", "text": f"DATABASE_URL={self.SECRET}\n"})
        response, code = run(encode(request), self.layout, self.system, {})
        self.assertEqual((code, response["result"]["keys"]), (0, ["DATABASE_URL"]))
        self.assertNotIn("s3cr3t", str(response))
        bad, code = run(encode(envelope("environment", arguments={"operation": "set", "text": "PORT=1\n", "x": 1})),
                        self.layout, self.system, {})
        self.assertEqual(code, 2)


class RegistryLoginTests(ProvisionTestCase):
    TOKEN = "ghp_exampleTOKEN1234567890"

    def login(self, app="orders", **changes):
        arguments = {"registry": "ghcr.io", "username": "deploy-bot", "token": self.TOKEN}
        arguments.update(changes)
        return manage_registry(self.layout, app, "login", **arguments)

    def config(self, app="orders"):
        return self.layout.registry_logins / app / "config.json"

    def test_login_writes_a_root_only_docker_config_for_that_application_and_returns_no_token(self):
        self.provision()
        result = self.login()
        self.assertEqual(result, {"registries": [{"registry": "ghcr.io", "username": "deploy-bot"}]})
        self.assertNotIn(self.TOKEN, str(result))
        document = json.loads(self.config().read_text())
        auth = base64.b64decode(document["auths"]["ghcr.io"]["auth"]).decode()
        self.assertEqual(auth, f"deploy-bot:{self.TOKEN}")
        self.assertEqual((stat.S_IMODE(self.config().stat().st_mode), stat.S_IMODE(self.config().parent.stat().st_mode)),
                         (0o600, 0o700))
        self.assertEqual(manage_registry(self.layout, "orders", "list"), result)

    def test_logins_are_per_application_and_logout_removes_the_file(self):
        self.provision("orders")
        self.provision("billing", public_key=ed25519(2))
        self.login("orders")
        self.assertFalse(self.config("billing").exists())
        self.assertEqual(manage_registry(self.layout, "billing", "list"), {"registries": []})
        manage_registry(self.layout, "orders", "logout", registry="ghcr.io")
        self.assertFalse(self.config().exists())
        manage_registry(self.layout, "orders", "logout", registry="ghcr.io")  # idempotent

    def test_invalid_input_is_rejected_without_echoing_the_token(self):
        self.provision()
        bad = [{"registry": "docker.io"}, {"registry": "ghcr.io/evil"}, {"username": "a b"}, {"username": ""},
               {"token": "has space"}, {"token": "x" * 513}, {"token": "tok\nen"}, {"token": None}]
        for change in bad:
            with self.subTest(change=str(change)[:30]), self.assertRaises(ProvisionError) as caught:
                self.login(**change)
            self.assertNotIn("TOKEN", str(caught.exception))
        with self.assertRaises(ProvisionError):
            manage_registry(self.layout, "orders", "merge", registry="ghcr.io")
        self.assertFalse(self.config().exists())

    def test_an_unprovisioned_application_is_refused(self):
        with self.assertRaisesRegex(ProvisionError, "not provisioned"):
            self.login("orders")

    def test_a_corrupt_login_file_is_reported_not_overwritten_silently(self):
        self.provision()
        self.login()
        self.config().write_text("{not json")
        with self.assertRaisesRegex(ProvisionError, "unreadable"):
            self.login()

    def test_through_the_admin_entry_point_and_removed_with_the_application(self):
        self.provision()
        request = envelope("registry", arguments={"operation": "login", "registry": "ghcr.io", "username": "u", "token": self.TOKEN})
        response, code = run(encode(request), self.layout, self.system, {})
        self.assertEqual((code, response["result"]), (0, {"registries": [{"registry": "ghcr.io", "username": "u"}]}))
        self.assertNotIn(self.TOKEN, str(response))
        with self.lock.acquire():
            removed = deprovision(self.layout, self.system, self.lock, "orders")["removed"]
        self.assertTrue(removed["registry_login"])
        self.assertFalse(self.config().parent.exists())


class DeprovisionTests(ProvisionTestCase):
    def test_removes_everything_it_created_and_is_repeatable(self):
        self.provision()
        with self.lock.acquire():
            result = deprovision(self.layout, self.system, self.lock, "orders")
        self.assertTrue(all(result["removed"].values()), result)
        self.assertEqual(self.system.users, {})
        for path in (self.layout.registrations / "orders.json", self.layout.sudoers_dir / "shiplord-orders",
                     self.layout.authorized_keys / "ship-orders", self.layout.app_state / "orders",
                     self.layout.environments / "orders"):
            self.assertFalse(path.exists(), path)
        with self.lock.acquire():
            again = deprovision(self.layout, self.system, self.lock, "orders")
        self.assertFalse(any(again["removed"].values()), again)

    def test_other_applications_are_untouched(self):
        self.provision("orders")
        self.provision("billing", public_key=ed25519(2))
        keep = {k: v for k, v in self.snapshot().items() if "billing" in k}
        with self.lock.acquire():
            deprovision(self.layout, self.system, self.lock, "orders")
        self.assertEqual({k: v for k, v in self.snapshot().items() if "billing" in k}, keep)
        self.assertIn("ship-billing", self.system.users)

    def open_store(self):
        state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, state_fd)
        return ReleaseState("orders", state_fd, self.lock)

    def test_refuses_while_a_release_is_deployed(self):
        self.provision()
        store = self.open_store()
        with self.lock.acquire():
            store.begin("request-1", release())
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                store.advance(phase)
            store.record_commit()
            store.clear_after_reconciliation()
            before = self.snapshot()
            with self.assertRaisesRegex(ProvisionError, "release is deployed"):
                deprovision(self.layout, self.system, self.lock, "orders")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("ship-orders", self.system.users)

    def deploy_one(self, store):
        store.begin("request-1", release())
        for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
            store.advance(phase)
        store.record_commit()
        store.clear_after_reconciliation()

    def test_retiring_stops_the_runtime_before_removal_and_discards_the_releases(self):
        self.provision()
        store = self.open_store()
        events = []
        with self.lock.acquire():
            self.deploy_one(store)
            result = deprovision(self.layout, self.system, self.lock, "orders",
                                 retire=lambda registration, state: events.append(
                                     (registration["application_id"], state["current"]["commit"])))
        self.assertEqual(events, [("orders", "a" * 40)])
        self.assertTrue(result["removed"]["account"])
        self.assertFalse((self.layout.registrations / "orders.json").exists())

    def test_a_failed_retirement_leaves_the_application_registered_and_reachable(self):
        self.provision()
        store = self.open_store()

        def failing(registration, state):
            raise ProvisionError("could not retire the application: route")
        with self.lock.acquire():
            self.deploy_one(store)
            before = self.snapshot()
            with self.assertRaisesRegex(ProvisionError, "could not retire"):
                deprovision(self.layout, self.system, self.lock, "orders", retire=failing)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("ship-orders", self.system.users)

    def test_refuses_with_an_unfinished_transaction(self):
        self.provision()
        store = self.open_store()
        with self.lock.acquire():
            store.begin("request-1", release())
            with self.assertRaisesRegex(ProvisionError, "unfinished transaction"):
                deprovision(self.layout, self.system, self.lock, "orders")
        self.assertTrue((self.layout.registrations / "orders.json").exists())

    def test_never_removes_an_account_it_did_not_create(self):
        self.provision()
        self.system.users["ship-orders"]["comment"] = "taken over"
        with self.lock.acquire(), self.assertRaisesRegex(ProvisionError, "not a Shiplord"):
            deprovision(self.layout, self.system, self.lock, "orders")
        self.assertIn("ship-orders", self.system.users)

    def test_userdel_failure_is_surfaced(self):
        self.provision()
        self.system.fail = {"userdel"}
        with self.lock.acquire(), self.assertRaisesRegex(ProvisionError, "could not remove the deployment account"):
            deprovision(self.layout, self.system, self.lock, "orders")

    def test_requires_the_host_lock(self):
        with self.assertRaisesRegex(ProvisionError, "lock"):
            deprovision(self.layout, self.system, self.lock, "orders")


def envelope(action="provision", **changes):
    value = {"version": 1, "request_id": "req-1", "action": action, "application_id": "orders", "commit": None,
             "arguments": {"runtime": "docker", "api_domain": "orders.example.com",
                           "image_repository": "ghcr.io/example/orders", "public_key": ed25519(1),
                           "key_label": "laptop"}}
    value.update(changes)
    return value


def encode(value):
    """Requests are plain dicts now; they travel as one frame on stdin."""
    return value


class AdminTests(ProvisionTestCase):
    def test_parse_accepts_a_valid_envelope_and_rejects_everything_else(self):
        self.assertEqual(parse_request(envelope())["action"], "provision")
        cases = {
            "not an object": [],
            "helper action": envelope("deploy"),
            "unknown action": envelope("shell"),
            "commit set": envelope(commit="a" * 40),
            "no application": envelope(application_id=None),
            "extra field": dict(envelope(), extra=1),
            "missing field": {k: v for k, v in envelope().items() if k != "arguments"},
            "bad request id": envelope(request_id="a b"),
        }
        for label, message in cases.items():
            with self.subTest(label), self.assertRaises(ProtocolError):
                parse_request(message)

    def test_serve_reads_one_frame_and_writes_one_frame_with_a_matching_request_id(self):
        def exchange(message, trailing=b""):
            source, output = io.BytesIO(), io.BytesIO()
            write_frame(source, message)
            code = serve(io.BytesIO(source.getvalue() + trailing), output, self.layout, self.system, {})
            return code, read_frame(io.BytesIO(output.getvalue()))
        code, response = exchange(envelope())
        self.assertEqual((code, response["status"], response["request_id"]), (0, "ok", "req-1"))
        self.assertEqual(exchange(envelope(), trailing=b"x")[0], 2)  # trailing input is refused
        garbage = io.BytesIO()
        self.assertEqual(serve(io.BytesIO(b"junk"), garbage, self.layout, self.system, {}), 2)
        self.assertEqual(read_frame(io.BytesIO(garbage.getvalue()))["error"], "invalid_request")

    def test_secrets_never_travel_in_argv_because_the_entry_point_takes_no_arguments(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, "-m", "shiplord.admin", "payload"], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)

    def test_provision_and_deprovision_through_the_admin_entry_point(self):
        response, code = run(encode(envelope()), self.layout, self.system, {})
        self.assertEqual((code, response["status"], response["request_id"]), (0, "ok", "req-1"))
        self.assertEqual(response["result"]["host_port"], 20000)
        response, code = run(encode(envelope("deprovision", arguments={})), self.layout, self.system, {})
        self.assertEqual((code, response["result"]["removed"]["registration"]), (0, True))

    def test_retire_must_be_the_boolean_true(self):
        run(encode(envelope()), self.layout, self.system, {})
        for value in ("yes", 1, False):
            with self.subTest(value=value):
                response, code = run(encode(envelope("deprovision", arguments={"retire": value})), self.layout, self.system, {})
                self.assertNotEqual(code, 0)
        self.assertTrue((self.layout.registrations / "orders.json").exists())

    def test_key_operations_through_the_admin_entry_point(self):
        run(encode(envelope()), self.layout, self.system, {})
        request = envelope("keys", arguments={"operation": "add", "label": "ci", "public_key": ed25519(2)})
        self.assertEqual(run(encode(request), self.layout, self.system, {})[0]["result"], {"changed": True})
        listing = run(encode(envelope("keys", arguments={"operation": "list"})), self.layout, self.system, {})[0]
        self.assertEqual([k["label"] for k in listing["result"]["keys"]], ["laptop", "ci"])

    def test_registered_deployment_accounts_are_denied(self):
        run(encode(envelope()), self.layout, self.system, {})
        uid = self.system.users["ship-orders"]["uid"]
        before = self.snapshot()
        response, code = run(encode(envelope("deprovision", arguments={})), self.layout, self.system,
                             {"SUDO_UID": str(uid)})
        self.assertEqual((code, response["error"]), (3, "denied"))
        self.assertEqual(self.snapshot(), before)
        response, code = run(encode(envelope("deprovision", arguments={})), self.layout, self.system,
                             {"SUDO_UID": "501"})
        self.assertEqual(code, 0)

    def test_errors_are_classified_by_exit_code(self):
        response, code = run(encode(envelope(version=2)), self.layout, self.system, {})
        self.assertEqual((code, response["status"], response["supported_versions"]), (4, "incompatible", [1]))
        for bad in ("garbage", encode(envelope(arguments={"runtime": "docker"})),
                    encode(envelope(arguments=dict(envelope()["arguments"], extra=1))),
                    encode(envelope(application_id="../x"))):
            with self.subTest(bad=str(bad)[:20]):
                self.assertEqual(run(bad, self.layout, self.system, {})[1], 2)
        failing = envelope()
        failing["arguments"]["public_key"] = "junk"
        response, code = run(encode(failing), self.layout, self.system, {})
        self.assertEqual((code, response["error"]), (1, "failed"))
        self.assertNotIn("junk", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
