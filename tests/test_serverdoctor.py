from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import shiplord  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.admin import parse_request, run  # noqa: E402
from shiplord.client import AdminClient  # noqa: E402
from shiplord.installer import bootstrap  # noqa: E402
from shiplord.protocol import ProtocolError  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState  # noqa: E402
from shiplord.serverdoctor import server_doctor  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from test_commands import FakeSsh, ok  # noqa: E402
from test_installer import FakeCaddy, InstallerTestCase  # noqa: E402
from test_provision import FakeSystem as AccountSystem, ed25519, envelope, release  # noqa: E402

SOURCE = Path(shiplord.__file__).parent


class HostSystem(AccountSystem):
    """Accounts plus the commands the doctor runs; each can be made to fail."""

    def __init__(self):
        super().__init__()
        self.failing: set[str] = set()

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        tool = argv[0].rsplit("/", 1)[-1]
        if tool in ("systemctl", "sshd", "docker"):
            self.calls.append(list(argv))
            return CommandResult(1 if tool in self.failing else 0, "27.3.1" if tool == "docker" else "")
        return super().run(argv, input=input, timeout=timeout)


class DoctorTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        self.system = HostSystem()
        self.caddy = FakeCaddy()
        bootstrap(self.layout, SOURCE, self.system, self.caddy)
        for directory in (self.layout.sudoers_dir, self.layout.registrations, self.layout.environments):
            directory.mkdir(parents=True, exist_ok=True)
        self.var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.var_fd)
        self.lock = HostLock(self.var_fd)

    def provision(self, app="orders"):
        from shiplord.provision import provision
        with self.lock.acquire():
            provision(self.layout, self.system, self.lock, application_id=app, runtime="docker", api_domain=f"{app}.example.com",
                      image_repository=f"ghcr.io/example/{app}", public_key=ed25519(1), key_label="laptop")

    def report(self):
        return server_doctor(self.layout, self.system, self.lock)

    def failing(self, report):
        return sorted(name for name, check in report["checks"].items() if not check["ok"])


class ServerDoctorTests(DoctorTestCase):
    def test_a_healthy_bootstrapped_server_with_an_application_reports_ok(self):
        self.provision()
        report = self.report()
        self.assertEqual((report["ok"], self.failing(report)), (True, []))
        self.assertEqual(report["checks"]["docker"]["version"], "27.3.1")
        self.assertRegex(report["checks"]["helper"]["version"], r"^\d+\.\d+\.\d+-[0-9a-f]{12}$")
        application = report["checks"]["applications"]["applications"][0]
        self.assertEqual((application["application"], application["deployed"], application["unfinished_transaction"]),
                         ("orders", False, None))

    def test_each_problem_is_reported_by_the_check_that_owns_it(self):
        cases = {
            "helper": lambda: (self.layout.lib / "current").unlink(),
            "caddy": lambda: (self.layout.caddy_root / "Caddyfile").write_text("example.org {\n}\n"),
            "sshd": lambda: self.layout.sshd_dropin.write_text("# edited by hand\n"),
        }
        for check, damage in cases.items():
            with self.subTest(check):
                bootstrap(self.layout, SOURCE, self.system, self.caddy) if check != "helper" else None
                damage()
                self.assertIn(check, self.failing(self.report()))
                self.setUp()

    def test_a_stopped_service_or_an_invalid_sshd_or_a_dead_docker_is_reported(self):
        self.system.failing = {"systemctl"}
        self.assertEqual(self.report()["checks"]["caddy"]["service_active"], False)

    def test_caddy_admin_api_reachability_is_reported_without_failing_the_check(self):
        with patch("shiplord.serverdoctor.http_get", side_effect=OSError):
            caddy = self.report()["checks"]["caddy"]
        self.assertEqual(caddy["admin_api"], False)
        with patch("shiplord.serverdoctor.http_get", return_value=(200, b"")):
            self.assertEqual(self.report()["checks"]["caddy"]["admin_api"], True)
        self.system.failing = {"sshd"}
        self.assertEqual(self.report()["checks"]["sshd"]["config_valid"], False)
        self.system.failing = {"docker"}
        report = self.report()
        self.assertEqual((report["checks"]["docker"]["ok"], report["ok"]), (False, False))

    def test_low_disk_is_reported_with_the_measured_free_space(self):
        with patch("shiplord.serverdoctor.shutil.disk_usage", return_value=type("U", (), {"free": 50 * 2**20})()):
            disk = self.report()["checks"]["disk"]
        self.assertFalse(disk["ok"])
        self.assertEqual({entry["free_mib"] for entry in disk["paths"]}, {50})

    def test_an_unfinished_transaction_or_unreadable_state_marks_that_application(self):
        self.provision()
        state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, state_fd)
        with self.lock.acquire():
            ReleaseState("orders", state_fd, self.lock).begin("req-1", release())
        application = self.report()["checks"]["applications"]["applications"][0]
        self.assertEqual((application["ok"], application["unfinished_transaction"]), (False, "prepared"))
        (self.layout.app_state / "orders" / "state.json").write_text("{}")
        application = self.report()["checks"]["applications"]["applications"][0]
        self.assertEqual((application["ok"], application["error"]), (False, "state is unreadable"))

    def test_the_report_names_no_secret_or_key_material(self):
        self.provision()
        text = str(self.report())
        for forbidden in ("AAAA", "ssh-ed25519", "PRIVATE KEY"):
            self.assertNotIn(forbidden, text)


class DoctorProtocolTests(DoctorTestCase):
    def test_only_the_doctor_may_omit_the_application(self):
        self.assertEqual(parse_request(envelope("doctor", application_id=None, arguments={}))["action"], "doctor")
        for message in (envelope("doctor", arguments={}), envelope("provision", application_id=None),
                        envelope("keys", application_id=None, arguments={"operation": "list"})):
            with self.subTest(action=message["action"]), self.assertRaises(ProtocolError):
                parse_request(message)

    def test_through_the_admin_entry_point_and_the_command_layer(self):
        response, code = run(envelope("doctor", application_id=None, arguments={}), self.layout, self.system, {})
        self.assertEqual((code, response["result"]["ok"]), (0, True))
        bad, code = run(envelope("doctor", application_id=None, arguments={"x": 1}), self.layout, self.system, {})
        self.assertEqual(code, 2)
        healthy = FakeSsh((ok({"ok": True, "checks": {"disk": {"ok": True}}}), b"", 0))
        self.assertEqual(commands.server_doctor(AdminClient("a@h", runner=healthy))[0], 0)
        broken = FakeSsh((ok({"ok": False, "checks": {"disk": {"ok": False}}}), b"", 0))
        code, result = commands.server_doctor(AdminClient("a@h", runner=broken))
        self.assertEqual((code, result["ok"]), (3, False))


if __name__ == "__main__":
    unittest.main()
