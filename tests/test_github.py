from __future__ import annotations

import base64
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from shiplord import github  # noqa: E402
from shiplord.client import ClientError  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.github import (SECRET_NAME, GhResult, GitHub, GitHubError, fetch_host_keys, generate_ci_key,  # noqa: E402
                             github_setup, github_status, known_hosts_lines, label_for, parse_host_key, resolve_ssh_host,
                             rotate_key)
from shiplord.keys import parse_public_key  # noqa: E402
from shiplord.manifest import validate_manifest  # noqa: E402
from shiplord.workflow import WORKFLOW_PATH, render_workflow  # noqa: E402

REF = "cd" * 20
HOST = "server.example.com"


def host_key(seed=1, key_type="ssh-ed25519"):
    def field(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value
    return key_type, base64.b64encode(field(key_type.encode()) + field(bytes([seed]) * 32)).decode()


def manifest(runtime="docker", with_github=False):
    value = {"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk", "runtime": runtime,
             "http": {"api_domain": "orders.example.com", "frontend_origins": [], "container_port": 8080,
                      "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
             "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}
    if with_github:
        value["github"] = {"repository": "Example/orders", "deployment_branch": "main"}
    return validate_manifest(value)


MANIFEST_TOML = '''schema_version = 1
name = "orders"
server = "production"
template = "google-adk"
runtime = "docker"
[http]
api_domain = "orders.example.com"
frontend_origins = []
container_port = 8080
readiness_path = "/ready"
[resources]
memory_mb = 512
[deploy]
readiness_timeout_seconds = 90
retain_successful_releases = 2
'''


class FakeGh:
    def __init__(self):
        self.calls, self.secrets, self.variables_, self.fail, self.permission = [], {}, {}, set(), "ADMIN"
        self.run = None

    def __call__(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        key = " ".join(argv[:2])
        if key in self.fail:
            return GhResult(1, "", f"{key} failed")
        if argv[:2] == ["api", "user"]:
            return GhResult(0, "octocat\n", "")
        if argv[:2] == ["repo", "view"]:
            return GhResult(0, json.dumps({"viewerPermission": self.permission}), "")
        if argv[:2] == ["secret", "set"]:
            self.secrets[argv[2]] = stdin
            return GhResult(0, "", "")
        if argv[:2] == ["variable", "set"]:
            self.variables_[argv[2]] = stdin.decode()
            return GhResult(0, "", "")
        if argv[:2] == ["secret", "list"]:
            return GhResult(0, json.dumps([{"name": n} for n in self.secrets]), "")
        if argv[:2] == ["variable", "list"]:
            return GhResult(0, json.dumps([{"name": n, "value": v} for n, v in self.variables_.items()]), "")
        if argv[:2] == ["run", "list"]:
            return GhResult(0, json.dumps([self.run] if self.run else []), "")
        raise AssertionError(argv)

    def order(self):
        return [" ".join(a[:2]) + (" " + a[2] if a[0] in ("secret", "variable") and a[1] == "set" else "")
                for a, _ in self.calls]


class FakeServer:
    """Registered CI keys, a fake admin channel, and an SSH runner that authenticates only registered keys."""

    def __init__(self):
        self.keys: dict[str, str] = {}
        self.admin_calls: list[tuple[str, dict]] = []
        self.fail_add = False
        self.fail_remove = set()
        self.ssh_events: list[str] = []

    def call(self, action, application_id, arguments):
        self.admin_calls.append((action, dict(arguments)))
        operation = arguments["operation"]
        if operation == "add":
            if self.fail_add:
                raise ClientError("the server refused the key", 3)
            self.keys[arguments["label"]] = parse_public_key(arguments["public_key"]).fingerprint
            return {"changed": True}
        if operation == "remove":
            if arguments["label"] in self.fail_remove:
                raise ClientError("could not remove", 1)
            self.keys.pop(arguments["label"], None)
            return {"changed": True}
        return {"keys": [{"label": k, "fingerprint": v} for k, v in self.keys.items()]}

    def runner(self, command, payload, timeout):
        identity = Path(command[command.index("-i") + 1])
        public = parse_public_key(Path(str(identity) + ".pub").read_text()).fingerprint
        known = next(part.split("=", 1)[1] for part in command if part.startswith("UserKnownHostsFile="))
        self.ssh_events.append(f"ssh:{public in self.keys.values()}:{Path(known).read_text().splitlines()[0].split()[0]}")
        request_id = read_frame(io.BytesIO(payload))["request_id"]
        message = {"status": "ok", "request_id": request_id, "version": 1, "helper_version": "0.1.0", "actions": ["deploy"]}
        if public not in self.keys.values():
            message = {"status": "error", "error": "access_denied"}
        out = io.BytesIO()
        write_frame(out, message)
        return out.getvalue(), b"", 0

    def admin(self):
        return self


class GitHubTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "ship.toml").write_text(MANIFEST_TOML)
        self.gh_runner, self.server = FakeGh(), FakeServer()
        self.gh = GitHub(self.gh_runner)

    def setup_call(self, **overrides):
        options = dict(manifest=manifest(), repository="Example/orders", project=self.project, admin=self.server, gh=self.gh,
                       admin_target="appuser@" + HOST, tooling_ref=REF, home=self.home, runner=self.server.runner,
                       host_keys=lambda target: [host_key(1), host_key(2, "ecdsa-sha2-nistp256")],
                       resolve=lambda host: (host, 22))
        options.update(overrides)
        manifest_value = options.pop("manifest")
        repository = options.pop("repository")
        return github_setup(manifest_value, repository, **options)

    def state(self):
        return json.loads((self.home / "shiplord" / "ci" / "orders" / "state.json").read_text())


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class SetupTests(GitHubTestCase):
    def test_setup_registers_proves_then_stores_the_key_sets_variables_and_writes_the_workflow(self):
        result = self.setup_call()
        order = self.gh_runner.order()
        self.assertLess(order.index("api user"), order.index("secret set SHIPLORD_SSH_PRIVATE_KEY"))
        self.assertEqual(self.server.admin_calls[0][0], "keys")
        self.assertEqual(self.server.ssh_events, [f"ssh:True:{HOST}"])  # proven against the pinned host key
        secret = self.gh_runner.secrets[SECRET_NAME]
        self.assertTrue(secret.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_SERVER_HOST"], HOST)
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_DEPLOY_USER"], "ship-orders")
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_SSH_KNOWN_HOSTS"],
                         known_hosts_lines(HOST, 22, [host_key(1), host_key(2, "ecdsa-sha2-nistp256")]))
        self.assertNotIn("SHIPLORD_SSH_PORT", self.gh_runner.variables_)
        workflow = (self.project / WORKFLOW_PATH).read_text()
        expected = render_workflow(manifest(with_github=True) | {"github": {"repository": "Example/orders",
                                   "deployment_branch": "main"}}, tooling_ref=REF)
        self.assertEqual(workflow, expected)
        self.assertIn('[github]\nrepository = "Example/orders"', (self.project / "ship.toml").read_text())
        self.assertTrue(result["ok"] and result["github_account"] == "octocat")
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})

    def test_the_private_key_never_leaves_the_machine_except_through_the_secret_and_is_then_deleted(self):
        result = self.setup_call()
        ci_directory = self.home / "shiplord" / "ci" / "orders"
        self.assertEqual([p.name for p in ci_directory.iterdir()], ["state.json"])
        self.assertEqual(oct(ci_directory.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((ci_directory / "state.json").stat().st_mode & 0o777), "0o600")
        key = self.gh_runner.secrets[SECRET_NAME].decode()
        blob = "".join(key.splitlines()[1:-1])
        everything = json.dumps(result) + json.dumps(self.state()) + json.dumps(self.server.admin_calls)
        everything += " ".join(" ".join(a) for a, _ in self.gh_runner.calls)
        self.assertNotIn(blob[:40], everything)
        for variable in self.gh_runner.variables_.values():
            self.assertNotIn(blob[:40], variable)
        self.assertIsNone(self.state()["private_key"])

    def test_repeating_setup_reuses_the_working_key(self):
        first = self.setup_call()
        before = dict(self.server.keys)
        second = self.setup_call()
        self.assertEqual(second["key_label"], first["key_label"])
        self.assertEqual(self.server.keys, before)
        self.assertEqual(len([c for c in self.server.admin_calls if c[1]["operation"] == "add"]), 1)
        self.assertEqual(len([a for a, _ in self.gh_runner.calls if a[:2] == ["secret", "set"]]), 1)
        self.assertEqual(second["steps"]["workflow"], f"unchanged ({WORKFLOW_PATH})")

    def test_missing_permission_or_authentication_stops_before_any_change(self):
        self.gh_runner.permission = "WRITE"
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual((caught.exception.code, caught.exception.step), (3, "access"))
        self.assertIn("ADMIN or MAINTAIN", str(caught.exception))
        self.assertEqual((self.server.admin_calls, self.gh_runner.secrets), ([], {}))
        self.gh_runner.fail.add("api user")
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual(caught.exception.code, 3)

    def test_a_github_section_already_in_the_file_is_never_duplicated(self):
        self.setup_call()
        text = (self.project / "ship.toml").read_text()
        self.assertEqual(text.count("[github]"), 1)
        self.setup_call(manifest=manifest())  # a caller holding a stale dict without the section
        self.assertEqual((self.project / "ship.toml").read_text(), text)
        (self.project / "ship.toml").write_text(text.replace("Example/orders", "Someone/else"))
        with self.assertRaisesRegex(GitHubError, "names Someone/else"):
            self.setup_call(manifest=manifest())

    def test_a_different_repository_than_the_manifest_names_is_refused(self):
        with self.assertRaisesRegex(GitHubError, "already names"):
            self.setup_call(manifest=manifest(with_github=True), repository="Other/repo")
        with self.assertRaisesRegex(GitHubError, "owner/repository"):
            self.setup_call(repository="not a repo")

    def test_a_failure_registering_leaves_the_key_for_resumption_and_never_stores_the_secret(self):
        self.server.fail_add = True
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual(caught.exception.step, "register")
        self.assertEqual(self.gh_runner.secrets, {})
        retained = self.state()["private_key"]
        self.assertTrue(Path(retained).is_file())
        self.assertEqual(oct(Path(retained).stat().st_mode & 0o777), "0o600")
        fingerprint = self.state()["fingerprint"]
        self.server.fail_add = False
        result = self.setup_call()
        self.assertEqual(result["key_fingerprint"], fingerprint)  # the same key was resumed, not replaced
        self.assertFalse(Path(retained).exists())
        self.assertIn(SECRET_NAME, self.gh_runner.secrets)

    def test_a_ci_key_the_server_rejects_is_never_stored_in_github(self):
        original = self.server.call

        def register_nothing(action, application_id, arguments):
            if arguments["operation"] == "add":
                return {"changed": True}  # pretends to register but does not
            return original(action, application_id, arguments)
        self.server.call = register_nothing
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual(caught.exception.step, "verify")
        self.assertEqual(self.gh_runner.secrets, {})
        self.assertIsNotNone(self.state()["private_key"])

    def test_a_failure_storing_the_secret_is_resumable(self):
        self.gh_runner.fail.add("secret set")
        with self.assertRaises(GitHubError):
            self.setup_call()
        self.assertFalse(self.state()["secret_set"])
        self.assertTrue(Path(self.state()["private_key"]).is_file())
        self.gh_runner.fail.clear()
        self.assertTrue(self.setup_call()["ok"])
        self.assertTrue(self.state()["secret_set"])

    def test_an_edited_workflow_is_not_overwritten_without_force(self):
        self.setup_call()
        path = self.project / WORKFLOW_PATH
        path.write_text(path.read_text() + "# my change\n")
        with self.assertRaisesRegex(GitHubError, "was edited"):
            self.setup_call(manifest=manifest(with_github=True))
        self.assertIn("# my change", path.read_text())
        self.setup_call(manifest=manifest(with_github=True), force_workflow=True)
        self.assertNotIn("# my change", path.read_text())

    def test_the_tooling_version_must_be_pinned_explicitly_when_it_cannot_be_detected(self):
        with patch("shiplord.github.detect_tooling_ref", return_value=None), self.assertRaisesRegex(GitHubError, "--tooling-ref"):
            self.setup_call(tooling_ref=None)
        with patch("shiplord.github.detect_tooling_ref", return_value="ef" * 20):
            self.assertIn("efefefefefef", self.setup_call(tooling_ref=None)["steps"]["workflow"])

    def test_native_applications_get_credentials_but_no_generated_workflow(self):
        (self.project / "ship.toml").write_text(MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"'))
        result = self.setup_call(manifest=manifest("native"))
        self.assertIn("skipped", result["steps"]["workflow"])
        self.assertFalse((self.project / WORKFLOW_PATH).exists())
        self.assertIn(SECRET_NAME, self.gh_runner.secrets)

    def test_tailscale_settings_a_custom_ci_host_and_a_nondefault_port_are_carried_through(self):
        result = self.setup_call(ci_host="tail.example.ts.net", resolve=lambda host: (host, 2222),
                                 tailscale={"SHIPLORD_TS_CLIENT_ID": "id123", "SHIPLORD_TS_AUDIENCE": "aud"})
        variables = self.gh_runner.variables_
        self.assertEqual((variables["SHIPLORD_TS_CLIENT_ID"], variables["SHIPLORD_SSH_PORT"], variables["SHIPLORD_SERVER_HOST"]),
                         ("id123", "2222", "tail.example.ts.net"))
        self.assertTrue(variables["SHIPLORD_SSH_KNOWN_HOSTS"].startswith("[tail.example.ts.net]:2222 ssh-ed25519 "))
        self.assertEqual(result["server_host"], "tail.example.ts.net")
        self.assertNotIn("tailscale", result["steps"])

    def test_the_remaining_manual_steps_are_stated_and_no_run_is_claimed(self):
        text = " ".join(self.setup_call()["remaining"])
        self.assertIn("Tailscale", text)
        self.assertIn("No GitHub Actions run has been performed", text)


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class RotationTests(GitHubTestCase):
    def rotate(self, **overrides):
        options = dict(admin=self.server, gh=self.gh, admin_target="appuser@" + HOST, home=self.home, runner=self.server.runner,
                       host_keys=lambda target: [host_key(1)], resolve=lambda host: (host, 22))
        options.update(overrides)
        return rotate_key(manifest(with_github=True), **options)

    def setUp(self):
        super().setUp()
        self.first = self.setup_call()

    def test_rotation_adds_proves_updates_github_and_only_then_revokes_the_old_key(self):
        old_secret = self.gh_runner.secrets[SECRET_NAME]
        result = self.rotate()
        self.assertNotEqual(result["key_label"], self.first["key_label"])
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})
        operations = [(c[1]["operation"], c[1]["label"]) for c in self.server.admin_calls[1:]]
        self.assertEqual(operations, [("add", result["key_label"]), ("remove", self.first["key_label"])])
        self.assertNotEqual(self.gh_runner.secrets[SECRET_NAME], old_secret)
        self.assertEqual(self.server.ssh_events[-1], f"ssh:True:{HOST}")
        self.assertEqual(self.state()["label"], result["key_label"])
        self.assertEqual([p.name for p in (self.home / "shiplord" / "ci" / "orders").iterdir()], ["state.json"])

    def test_a_key_that_fails_verification_is_removed_and_the_old_key_stays_authoritative(self):
        old_secret = self.gh_runner.secrets[SECRET_NAME]
        original = self.server.call

        def register_nothing(action, application_id, arguments):
            if arguments["operation"] == "add":
                return {"changed": True}
            return original(action, application_id, arguments)
        self.server.call = register_nothing
        with self.assertRaises(GitHubError):
            self.rotate(admin=self.server)
        self.assertEqual(self.gh_runner.secrets[SECRET_NAME], old_secret)
        self.assertEqual(set(self.server.keys), {self.first["key_label"]})
        self.assertEqual(self.state()["label"], self.first["key_label"])

    def test_a_github_failure_leaves_the_old_key_working_and_unregisters_the_unused_new_one(self):
        old_secret = self.gh_runner.secrets[SECRET_NAME]
        self.gh_runner.fail.add("secret set")
        with self.assertRaises(GitHubError):
            self.rotate()
        self.assertEqual(self.gh_runner.secrets[SECRET_NAME], old_secret)
        self.assertEqual(set(self.server.keys), {self.first["key_label"]})
        self.assertEqual(self.state()["label"], self.first["key_label"])
        self.assertEqual([p.name for p in (self.home / "shiplord" / "ci" / "orders").iterdir()], ["state.json"])

    def test_a_failed_revocation_is_reported_loudly_with_the_new_key_already_live(self):
        self.server.fail_remove = {self.first["key_label"]}
        with self.assertRaises(GitHubError) as caught:
            self.rotate()
        self.assertEqual(caught.exception.step, "revoke")
        self.assertIn(self.first["key_label"], str(caught.exception))
        self.assertEqual(len(self.server.keys), 2)

    def test_rotation_needs_a_completed_setup(self):
        shutil.rmtree(self.home)
        with self.assertRaisesRegex(GitHubError, "run `shiplord github setup` first"):
            self.rotate()


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class StatusTests(GitHubTestCase):
    def status(self):
        return github_status(manifest(with_github=True), project=self.project, gh=self.gh, admin=self.server, home=self.home)

    def test_a_fully_configured_repository_reports_ok_except_the_missing_run(self):
        self.setup_call(tailscale={"SHIPLORD_TS_CLIENT_ID": "id", "SHIPLORD_TS_AUDIENCE": "aud"})
        code, result = self.status()
        failing = [name for name, check in result["checks"].items() if not check["ok"]]
        self.assertEqual((code, failing), (3, ["last_run"]))
        self.assertEqual(result["checks"]["last_run"]["note"], "no workflow run has happened yet")
        self.gh_runner.run = {"status": "completed", "conclusion": "success", "headSha": "a" * 40, "url": "u", "createdAt": "t"}
        code, result = self.status()
        self.assertEqual((code, result["ok"]), (0, True))

    def test_each_problem_is_named(self):
        self.setup_call()
        del self.gh_runner.secrets[SECRET_NAME]
        del self.gh_runner.variables_["SHIPLORD_DEPLOY_USER"]
        self.server.keys.clear()
        (self.project / WORKFLOW_PATH).unlink()
        self.gh_runner.permission = "READ"
        code, result = self.status()
        checks = result["checks"]
        self.assertEqual(code, 3)
        for name in ("access", "secret", "variables", "workflow", "server_key", "tailscale"):
            self.assertFalse(checks[name]["ok"], name)
        self.assertEqual(checks["variables"]["missing"], ["SHIPLORD_DEPLOY_USER"])

    def test_a_manifest_without_github_is_a_usage_error(self):
        with self.assertRaises(GitHubError) as caught:
            github_status(manifest(), project=self.project, gh=self.gh, home=self.home)
        self.assertEqual(caught.exception.code, 2)


class HostKeyTests(unittest.TestCase):
    def test_host_keys_are_validated_and_formatted_for_known_hosts(self):
        key_type, blob = host_key(3)
        self.assertEqual(parse_host_key(f"{key_type} {blob} root@server"), (key_type, blob))
        for bad in ("", "ssh-ed25519", "ssh-dss AAAA", f"ecdsa-sha2-nistp256 {blob}", "ssh-ed25519 !!!", "command=x ssh-ed25519 AAAA"):
            with self.subTest(bad=bad), self.assertRaises(GitHubError):
                parse_host_key(bad)
        self.assertEqual(known_hosts_lines("h.example.com", 22, [(key_type, blob)]), f"h.example.com {key_type} {blob}\n")
        self.assertEqual(known_hosts_lines("h.example.com", 2222, [(key_type, blob)]), f"[h.example.com]:2222 {key_type} {blob}\n")

    def test_the_host_keys_are_read_over_the_trusted_ssh_connection_not_a_keyscan(self):
        key_type, blob = host_key(4)
        seen = []

        def runner(command, payload, timeout):
            seen.append(command)
            return f"{key_type} {blob} root@s\n".encode(), b"", 0
        self.assertEqual(fetch_host_keys("appuser@server", runner), [(key_type, blob)])
        self.assertIn("StrictHostKeyChecking=yes", seen[0])
        self.assertIn("ssh_host_ed25519_key.pub", seen[0][-1])
        self.assertNotIn("keyscan", " ".join(seen[0]))
        with self.assertRaises(GitHubError):
            fetch_host_keys("appuser@server", lambda c, p, t: (b"", b"", 1))
        with self.assertRaises(GitHubError):
            fetch_host_keys("appuser@server", lambda c, p, t: (b"garbage line\n", b"", 0))

    @unittest.skipUnless(shutil.which("ssh"), "ssh is required")
    def test_an_alias_resolves_through_the_users_own_ssh_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text("Host prodalias\n  HostName real.example.com\n  Port 2200\n")
            self.assertEqual(resolve_ssh_host("prodalias", config), ("real.example.com", 2200))
            self.assertEqual(resolve_ssh_host("plain.example.com", config), ("plain.example.com", 22))
        with self.assertRaises(GitHubError):
            resolve_ssh_host("bad host; id")


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class KeyGenerationTests(unittest.TestCase):
    def test_a_private_ed25519_key_is_generated_and_labelled_from_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            private, public = generate_ci_key(Path(directory), "orders", "key-1")
            self.assertEqual(oct(private.stat().st_mode & 0o777), "0o600")
            key = parse_public_key(public)
            self.assertEqual(key.type, "ssh-ed25519")
            self.assertRegex(label_for(key.fingerprint), r"^ci-[a-z0-9]{8}$")
            other, _ = generate_ci_key(Path(directory), "orders", "key-2")
            self.assertNotEqual(private.read_bytes(), other.read_bytes())

    def test_the_gh_wrapper_sends_values_on_stdin_never_in_arguments(self):
        calls = []
        gh = GitHub(lambda argv, stdin=None: (calls.append((argv, stdin)), GhResult(0, "", ""))[1])
        gh.set_secret("o/r", "NAME", b"super-secret-value")
        gh.set_variable("o/r", "VAR", "multi\nline")
        for argv, stdin in calls:
            self.assertNotIn("super-secret-value", " ".join(argv))
            self.assertNotIn("multi", " ".join(argv))
        self.assertEqual([stdin for _, stdin in calls], [b"super-secret-value", b"multi\nline"])


if __name__ == "__main__":
    unittest.main()
