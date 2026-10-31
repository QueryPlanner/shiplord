"""Restricted-sudo integration; runs only on a disposable Ubuntu CI runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import shiplord
from shiplord.installer import helper_modules


@unittest.skipUnless(os.environ.get("SHIPLORD_TEST_SUDO") == "1", "requires disposable Ubuntu sudo runner")
class SudoIntegrationTests(unittest.TestCase):
    @classmethod
    def run_root(cls, *arguments):
        return subprocess.run(["sudo", "-n", *map(str, arguments)], check=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise RuntimeError("The restricted-sudo test requires Linux")
        cls.temp = tempfile.TemporaryDirectory(prefix="shiplord-sudo-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.root.chmod(0o711)
        cls.users = [f"shipa{os.getpid()}", f"shipb{os.getpid()}"]
        for user in cls.users:
            cls.run_root("useradd", "-M", "-d", cls.root, "-s", "/bin/sh", user)
            cls.addClassCleanup(cls.remove_user, user)
            cls.run_root("usermod", "-p", "invalid-hash", user)
        cls.uids = [int(subprocess.check_output(["id", "-u", user], text=True).strip()) for user in cls.users]
        cls.payload = Path("/usr/local/lib") / f"shiplord-sudo-ci-{os.getpid()}"
        cls.registration = Path("/etc/shiplord")
        cls.rule = Path("/etc/sudoers.d") / f"shiplord-ci-{os.getpid()}"
        for path in (cls.payload, cls.registration, cls.rule):
            if os.path.lexists(path):
                raise RuntimeError("Disposable sudo fixture path already exists")
        cls.run_root("install", "-d", "-m", "0755", cls.payload / "shiplord")
        cls.addClassCleanup(cls.remove_path, cls.payload)
        cls.run_root("install", "-d", "-m", "0755", cls.registration / "apps")
        cls.addClassCleanup(cls.remove_path, cls.registration)
        source = Path(shiplord.__file__).parent
        for name in helper_modules(source):  # exactly what the real installer ships
            cls.run_root("install", "-m", "0644", source / name, cls.payload / "shiplord" / name)
        launcher = cls.root / "launcher.py"
        launcher.write_text(f'import sys\nsys.path.insert(0, "{cls.payload}")\nfrom shiplord.privileged_host import main\nmain()\n')
        cls.run_root("install", "-m", "0644", launcher, cls.payload / "launcher.py")
        wrapper = cls.root / "wrapper"
        wrapper.write_text(f'#!/bin/sh\nexec /usr/bin/python3 -I -S -B "{cls.payload / "launcher.py"}"\n')
        cls.helper = cls.payload / "helper"
        cls.run_root("install", "-m", "0755", wrapper, cls.helper)
        for number, (app, user, uid) in enumerate(zip(("orders", "billing"), cls.users, cls.uids)):
            path = cls.root / f"{app}.json"
            path.write_text(json.dumps({"schema_version": 1, "application_id": app,
                                        "deployment_uid": uid, "deployment_user": user, "runtime": "docker",
                                        "api_domain": f"{app}.example.com", "host_port": 20000 + number,
                                        "image_repository": f"ghcr.io/example/{app}", "max_memory_mb": 4096}))
            cls.run_root("install", "-m", "0644", path, cls.registration / "apps" / path.name)
        policy = cls.root / "sudoers"
        policy.write_text("".join(f'{user} ALL=(root) NOPASSWD: NOSETENV: {cls.helper} ""\n' for user in cls.users))
        cls.run_root("visudo", "-cf", policy)
        cls.run_root("install", "-m", "0440", policy, cls.rule)
        cls.addClassCleanup(cls.remove_path, cls.rule)

    @classmethod
    def remove_path(cls, path):
        cls.run_root("rm", "-rf", "--", path)

    @classmethod
    def remove_user(cls, user):
        cls.run_root("userdel", user)

    def sudo(self, user, app, *extra, environment=()):
        request = {"version": 1, "request_id": "request-1", "action": "capabilities",
                   "application_id": app, "commit": None, "arguments": {}}
        body = json.dumps(request).encode()
        command = ["sudo", "-n", "-u", user, "/usr/bin/env", *environment,
                   "/usr/bin/sudo", "-n", *extra, str(self.helper)]
        return subprocess.run(command, input=len(body).to_bytes(4, "big") + body,
                              capture_output=True, timeout=20, check=False)

    def parse(self, result):
        size = int.from_bytes(result.stdout[:4], "big")
        self.assertEqual(len(result.stdout), size + 4)
        return json.loads(result.stdout[4:])

    def test_effective_sudo_policy_binds_each_account_to_its_app(self):
        for user, own, other in ((self.users[0], "orders", "billing"), (self.users[1], "billing", "orders")):
            with self.subTest(user=user):
                allowed = self.sudo(user, own)
                self.assertEqual(allowed.returncode, 0)
                self.assertEqual(self.parse(allowed)["actions"],
                         ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover",
                          "docker_storage_v1", "docker_shutdown_v1", "docker_generic_v1"])
                denied = self.sudo(user, other)
                self.assertEqual(denied.returncode, 3)
                self.assertEqual(self.parse(denied)["error"], "access_denied")

    def test_forged_inherited_sudo_identity_cannot_cross_app_boundary(self):
        forged = (f"SUDO_UID={self.uids[1]}", f"SUDO_USER={self.users[1]}")
        allowed = self.sudo(self.users[0], "orders", environment=forged)
        self.assertEqual(allowed.returncode, 0)
        denied = self.sudo(self.users[0], "billing", environment=forged)
        self.assertEqual(denied.returncode, 3)
        self.assertEqual(self.parse(denied)["error"], "access_denied")

    def test_sudo_policy_denies_environment_preservation_arguments_and_shell(self):
        self.assertNotEqual(self.sudo(self.users[0], "orders", "-E").returncode, 0)
        request = b""
        for command in ([str(self.helper), "unexpected"], ["/bin/sh", "-c", "true"],
                        ["SUDO_UID=0", str(self.helper)]):
            with self.subTest(command=command):
                result = subprocess.run(["sudo", "-n", "-u", self.users[0], "/usr/bin/sudo", "-n", *command],
                                        input=request, capture_output=True, timeout=10, check=False)
                self.assertNotEqual(result.returncode, 0)

    def test_unsafe_registration_mode_owner_and_symlink_are_denied(self):
        registration = self.registration / "apps/orders.json"
        original = self.root / "orders.json"
        try:
            self.run_root("chmod", "0666", registration)
            self.assertEqual(self.sudo(self.users[0], "orders").returncode, 3)
            self.run_root("chmod", "0644", registration)
            self.run_root("chown", self.users[0], registration)
            self.assertEqual(self.sudo(self.users[0], "orders").returncode, 3)
            self.run_root("chown", "root:root", registration)
            self.run_root("rm", "--", registration)
            self.run_root("ln", "-s", self.registration / "apps/billing.json", registration)
            self.assertEqual(self.sudo(self.users[0], "orders").returncode, 3)
        finally:
            self.run_root("rm", "-f", "--", registration)
            self.run_root("install", "-m", "0644", original, registration)

    def test_privileged_helper_times_out_when_caller_withholds_eof(self):
        command = ["sudo", "-n", "-u", self.users[0], "/usr/bin/sudo", "-n", str(self.helper)]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            self.assertEqual(process.wait(timeout=20), 3)
            data = process.stdout.read()
            size = int.from_bytes(data[:4], "big")
            self.assertEqual(json.loads(data[4:4 + size])["error"], "timeout")
