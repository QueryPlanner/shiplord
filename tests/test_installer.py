from __future__ import annotations

import ast
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import shiplord
from shiplord.installer import CLIENT_MODULES, BootstrapError, bootstrap, helper_modules
from shiplord.layout import Layout, deployment_user
from shiplord.routing import RoutingError
from shiplord.system import CommandResult

SOURCE = Path(shiplord.__file__).parent


class FakeSystem:
    def __init__(self):
        self.calls = []
        self.results = {}

    def run(self, argv, *, input=None, timeout=60):
        self.calls.append(list(argv))
        return self.results.get(argv[-1] if argv[0].endswith("systemctl") else argv[0].rsplit("/", 1)[-1],
                                CommandResult(0, ""))


class FakeCaddy:
    def __init__(self):
        self.validated = 0
        self.reloaded = 0
        self.fail_reload = False

    def validate(self, config):
        self.validated += 1

    def reload(self):
        self.reloaded += 1
        if self.fail_reload:
            raise RoutingError("failed", "reload")


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name).resolve()
        self.layout = Layout(self.prefix)
        caddy = self.layout.caddy_root
        caddy.mkdir(parents=True)
        caddy.chmod(0o755)
        (caddy / "Caddyfile").write_text("example.org {\n}\n")
        (caddy / "Caddyfile").chmod(0o644)
        self.system = FakeSystem()
        self.caddy = FakeCaddy()

    def run_bootstrap(self, source=SOURCE):
        return bootstrap(self.layout, source, self.system, self.caddy)


class ModuleSetTests(unittest.TestCase):
    def test_every_installed_module_uses_only_the_standard_library_and_the_package(self):
        allowed = set(sys.stdlib_module_names)
        for name in helper_modules(SOURCE):
            tree = ast.parse((SOURCE / name).read_text())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [(node.module or "").split(".")[0]]
                for module in modules:
                    with self.subTest(module_file=name, imported=module):
                        self.assertIn(module, allowed)

    def test_client_modules_are_excluded_and_exist(self):
        names = set(helper_modules(SOURCE))
        self.assertTrue(CLIENT_MODULES.isdisjoint(names))
        for client in CLIENT_MODULES:
            self.assertTrue((SOURCE / client).exists(), client)
        self.assertTrue({"privileged_host.py", "dispatch.py", "registry.py", "routing.py"} <= names)


class BootstrapTests(InstallerTestCase):
    def test_creates_layout_installs_code_and_wires_caddy_and_sshd(self):
        result = self.run_bootstrap()
        self.assertTrue(all(result["changed"].values()), result)
        layout = self.layout
        for path, mode in ((layout.registrations, 0o700), (layout.environments, 0o700), (layout.app_state, 0o700),
                           (layout.jobs, 0o700), (layout.authorized_keys, 0o755), (layout.var, 0o755)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        current = layout.lib / "current"
        self.assertEqual(os.readlink(current), result["version"])
        self.assertTrue((current / "shiplord" / "privileged_host.py").exists())
        self.assertFalse((current / "shiplord" / "cli.py").exists())
        self.assertIn("from shiplord.privileged_host import main", (current / "host_launcher.py").read_text())
        for wrapper, launcher in ((layout.host, "host_launcher.py"), (layout.admin, "admin_launcher.py"),
                                  (layout.worker, "worker_launcher.py"), (layout.dispatch, "dispatch_launcher.py")):
            self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
            self.assertEqual(wrapper.read_text(),
                             f'#!/bin/sh\nexec /usr/bin/python3 -I -S -B {layout.lib}/current/{launcher} "$@"\n')
        self.assertIn(f"import {layout.caddy_root}/ship/*.caddy", (layout.caddy_root / "Caddyfile").read_text())
        self.assertTrue((layout.caddy_root / "ship" / "00-shiplord.caddy").exists())
        self.assertEqual((self.caddy.validated, self.caddy.reloaded), (1, 1))
        dropin = layout.sshd_dropin.read_text()
        self.assertIn(f"AuthorizedKeysFile {layout.authorized_keys}/%u", dropin)
        self.assertIn("PermitTTY no", dropin)
        self.assertEqual(self.system.calls, [["/usr/sbin/sshd", "-t"],
                                              ["/usr/bin/systemctl", "try-reload-or-restart", "ssh.service"]])

    def test_second_run_changes_nothing(self):
        first = self.run_bootstrap()
        before = {str(p): p.read_bytes() for p in self.prefix.rglob("*") if p.is_file()}
        calls = len(self.system.calls)
        second = self.run_bootstrap()
        self.assertEqual(second["version"], first["version"])
        self.assertFalse(any(second["changed"].values()), second)
        self.assertEqual({str(p): p.read_bytes() for p in self.prefix.rglob("*") if p.is_file()}, before)
        self.assertEqual(len(self.system.calls), calls)
        self.assertEqual(self.caddy.reloaded, 1)

    def test_changed_code_installs_a_new_version_and_keeps_the_old_one(self):
        first = self.run_bootstrap()
        source = Path(self.temp.name) / "pkg"
        source.mkdir()
        for name in helper_modules(SOURCE):
            (source / name).write_bytes((SOURCE / name).read_bytes())
        (source / "dispatch.py").write_text((SOURCE / "dispatch.py").read_text() + "\n# changed\n")
        second = self.run_bootstrap(source)
        self.assertNotEqual(second["version"], first["version"])
        self.assertTrue(second["changed"]["code"] and second["changed"]["current"])
        self.assertFalse(second["changed"]["caddy_import"])
        self.assertEqual(os.readlink(self.layout.lib / "current"), second["version"])
        self.assertTrue((self.layout.lib / first["version"] / ".complete").exists())

    def test_tampered_installed_version_is_not_trusted(self):
        result = self.run_bootstrap()
        target = self.layout.lib / result["version"] / "shiplord" / "dispatch.py"
        target.write_text("raise SystemExit(0)\n")
        with self.assertRaisesRegex(BootstrapError, "do not match"):
            self.run_bootstrap()

    def test_non_root_cannot_bootstrap_the_real_host(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaisesRegex(BootstrapError, "root"):
            bootstrap(Layout(), SOURCE, self.system, self.caddy)

    def test_refuses_symlinked_targets(self):
        self.run_bootstrap()
        self.layout.host.unlink()
        self.layout.host.symlink_to("/bin/sh")
        with self.assertRaisesRegex(BootstrapError, "symbolic link"):
            self.run_bootstrap()

    def test_missing_caddy_is_a_clear_failure_after_code_install(self):
        (self.layout.caddy_root / "Caddyfile").unlink()
        self.layout.caddy_root.rmdir()
        with self.assertRaisesRegex(BootstrapError, "Caddy is not installed"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_caddy_failure_stops_before_touching_sshd(self):
        self.caddy.fail_reload = True
        with self.assertRaisesRegex(BootstrapError, "Caddy integration failed"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())
        self.assertEqual(self.system.calls, [])


class SshdTests(InstallerTestCase):
    def test_rejected_sshd_configuration_is_removed(self):
        self.system.results["sshd"] = CommandResult(1, "bad")
        with self.assertRaisesRegex(BootstrapError, "rejected"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_rejected_update_restores_the_previous_dropin(self):
        self.run_bootstrap()
        previous = self.layout.sshd_dropin.read_bytes()
        self.layout.sshd_dropin.write_text("# older\n")
        self.system.results["sshd"] = CommandResult(1, "bad")
        with self.assertRaises(BootstrapError):
            self.run_bootstrap()
        self.assertEqual(self.layout.sshd_dropin.read_text(), "# older\n")
        self.assertNotEqual(previous, b"# older\n")

    def test_failed_reload_restores_and_reports(self):
        self.system.results["ssh.service"] = CommandResult(1, "failed")
        with self.assertRaisesRegex(BootstrapError, "reload"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())


class DeploymentUserTests(unittest.TestCase):
    def test_names_are_short_deterministic_and_distinct(self):
        self.assertEqual(deployment_user("orders"), "ship-orders")
        long_a, long_b = "a" * 40, "a" * 39 + "b"
        names = {deployment_user(long_a), deployment_user(long_b)}
        self.assertEqual(len(names), 2)
        for name in names | {deployment_user("orders")}:
            self.assertLessEqual(len(name), 32)
            self.assertRegex(name, r"^ship-[a-z0-9-]+$")
        self.assertEqual(deployment_user(long_a), deployment_user(long_a))
        with self.assertRaises(ValueError):
            deployment_user("../x")


if __name__ == "__main__":
    unittest.main()
