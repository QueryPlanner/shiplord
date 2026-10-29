"""Bootstrap, provisioning, and the SSH forced-command path on a real Linux host.

This changes /etc, /usr/local, and system accounts, so it runs only when
SHIPLORD_TEST_HOST=1 AND the host is a disposable container or GitHub runner.
Container runs have no systemd: systemctl calls are stubbed, everything else is real.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import shiplord
from shiplord.admin import run as admin_run
from shiplord.framing import read_frame, write_frame
from shiplord.installer import bootstrap
from shiplord.layout import Layout
from shiplord.protocol import PROTOCOL_VERSION
from shiplord.routing import SubprocessCaddy
from shiplord.system import CommandResult, HostSystem

DISPOSABLE = os.path.exists("/.dockerenv") or os.environ.get("GITHUB_ACTIONS") == "true"
ENABLED = (os.environ.get("SHIPLORD_TEST_HOST") == "1" and DISPOSABLE and os.geteuid() == 0
           and shutil.which("caddy") and shutil.which("sshd"))
SOURCE = Path(shiplord.__file__).parent
PORT = 2222


class NoSystemd(HostSystem):
    """Real commands, except systemctl, which a container cannot run."""

    def run(self, argv, **kwargs):
        if argv[0].endswith("systemctl"):
            return CommandResult(0, "")
        return super().run(argv, **kwargs)


class LiveCaddy(SubprocessCaddy):
    """Real validate; reload through the admin API of the Caddy this test started."""

    def reload(self) -> None:
        self._run([self.caddy, "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], "reload")


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def ssh(key: Path, known_hosts: Path, user: str, *remote: str, stdin: bytes = b"", extra=()):
    command = ["ssh", "-p", str(PORT), "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
               *extra, f"{user}@127.0.0.1", *remote]
    return subprocess.run(command, input=stdin, capture_output=True, timeout=60)


def capabilities_frame(application_id):
    buffer = io.BytesIO()
    write_frame(buffer, {"version": PROTOCOL_VERSION, "request_id": "probe-1", "action": "capabilities",
                         "application_id": application_id, "commit": None, "arguments": {}})
    return buffer.getvalue()


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_HOST=1 in a disposable container or CI runner as root")
class HostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name)
        cls.server = subprocess.Popen(["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.server.kill)
        for _ in range(100):
            if subprocess.run(["caddy", "environ"], capture_output=True).returncode == 0 and _port_open(2019):
                break
            time.sleep(0.1)
        subprocess.run(["ssh-keygen", "-A"], check=True, capture_output=True)  # sshd -t needs host keys
        os.makedirs("/run/sshd", exist_ok=True)
        cls.layout = Layout()
        cls.caddy = LiveCaddy(caddy=shutil.which("caddy"))
        cls.system = NoSystemd()
        cls.first = bootstrap(cls.layout, SOURCE, cls.system, cls.caddy)
        cls.client_key = cls.work / "client"
        subprocess.run(["ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", str(cls.client_key)], check=True)
        cls.provisioned = cls.admin("provision", "orders", {
            "runtime": "docker", "api_domain": "orders.example.com", "image_repository": "ghcr.io/example/orders",
            "public_key": (cls.work / "client.pub").read_text(), "key_label": "laptop"})
        cls.sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-p", str(PORT), "-o", "ListenAddress=127.0.0.1"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.addClassCleanup(cls.sshd.kill)
        host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2]
        cls.known_hosts = cls.work / "known_hosts"
        cls.known_hosts.write_text(f"[127.0.0.1]:{PORT} {host_key[0]} {host_key[1]}\n")
        for _ in range(50):
            if subprocess.run(["ssh-keyscan", "-p", str(PORT), "127.0.0.1"], capture_output=True).stdout:
                break
            time.sleep(0.1)

    @staticmethod
    def admin(action, app, arguments):
        request = {"version": 1, "request_id": "admin-1", "action": action, "application_id": app, "commit": None,
                   "arguments": arguments}
        response, code = admin_run(request, Layout(), NoSystemd(), {})
        if code != 0:
            raise AssertionError(f"admin {action} failed: {response}")
        return response["result"]

    def login(self, *remote, stdin=b"", extra=(), user="ship-orders"):
        return ssh(self.client_key, self.known_hosts, user, *remote, stdin=stdin, extra=extra)

    def test_bootstrap_installed_a_root_owned_helper_and_wired_caddy_and_sshd(self):
        self.assertTrue(self.first["changed"]["code"] and self.first["changed"]["sshd"])
        for path in (self.layout.host, self.layout.admin, self.layout.dispatch, self.layout.lib / "current"):
            info = path.lstat()
            self.assertEqual(info.st_uid, 0, path)
            if not path.is_symlink():  # a symlink's own mode is always 0777
                self.assertFalse(info.st_mode & 0o022, path)
        self.assertIn("import /etc/caddy/ship/*.caddy", Path("/etc/caddy/Caddyfile").read_text())
        self.assertTrue(subprocess.run(["caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter",
                                        "caddyfile"], capture_output=True).returncode == 0)

    def test_sshd_match_block_applies_to_deployment_accounts_only(self):
        def effective(user):
            output = subprocess.run(["sshd", "-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"],
                                    capture_output=True, text=True, check=True).stdout
            return dict(line.split(" ", 1) for line in output.splitlines() if " " in line)
        deployment, other = effective("ship-orders"), effective("someoneelse")
        self.assertEqual(deployment["authorizedkeysfile"], "/etc/shiplord/authorized_keys/%u")
        self.assertEqual((deployment["permittty"], deployment["allowtcpforwarding"], deployment["passwordauthentication"]),
                         ("no", "no", "no"))
        self.assertNotIn("shiplord", other["authorizedkeysfile"])
        self.assertEqual(other["permittty"], "yes")  # the Match block did not leak into other accounts

    def test_repeating_bootstrap_and_provision_changes_nothing(self):
        self.assertFalse(any(bootstrap(self.layout, SOURCE, self.system, self.caddy)["changed"].values()))
        again = self.admin("provision", "orders", {
            "runtime": "docker", "api_domain": "orders.example.com", "image_repository": "ghcr.io/example/orders",
            "public_key": (self.work / "client.pub").read_text(), "key_label": "laptop"})
        self.assertFalse(any(again["created"].values()), again)
        self.assertEqual(again["host_port"], self.provisioned["host_port"])

    def test_forced_command_reaches_the_privileged_helper_for_its_own_application(self):
        result = self.login("/usr/local/libexec/shiplord-dispatch", stdin=capabilities_frame("orders"))
        self.assertEqual(result.returncode, 0, result.stderr)
        response = read_frame(io.BytesIO(result.stdout))
        self.assertEqual(response["status"], "ok")
        self.assertIn("deploy", response["actions"])

    def test_other_applications_and_server_scope_are_denied_by_the_helper(self):
        for application in ("billing", None):
            with self.subTest(application=application):
                result = self.login("/usr/local/libexec/shiplord-dispatch", stdin=capabilities_frame(application))
                self.assertEqual(result.returncode, 3, result.stderr)  # access_denied

    def test_the_client_cannot_run_other_commands_or_get_a_tty(self):
        for command in (["id"], ["sh", "-c", "echo hi"], ["/usr/local/libexec/shiplord-dispatch; id"], ["scp", "-t", "/tmp"]):
            with self.subTest(command=command):
                result = self.login(*command)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(b"uid=", result.stdout)
        tty = self.login("/usr/local/libexec/shiplord-dispatch", extra=("-tt",))
        self.assertNotIn(b"uid=", tty.stdout)
        forward = self.login("/usr/local/libexec/shiplord-dispatch", stdin=capabilities_frame("orders"),
                             extra=("-R", "19999:127.0.0.1:22", "-o", "ExitOnForwardFailure=yes"))
        self.assertEqual(forward.returncode, 255, forward.stderr)  # the forwarding request is refused

    def test_an_unregistered_key_cannot_log_in(self):
        stranger = self.work / "stranger"
        subprocess.run(["ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", str(stranger)], check=True)
        result = ssh(stranger, self.known_hosts, "ship-orders", "/usr/local/libexec/shiplord-dispatch",
                     stdin=capabilities_frame("orders"))
        self.assertEqual(result.returncode, 255)

    def test_the_deployment_account_cannot_change_its_own_access(self):
        def as_account(script):
            return subprocess.run(["su", "-s", "/bin/sh", "ship-orders", "-c", script], capture_output=True, timeout=30)
        for script in ("echo x >> /etc/shiplord/authorized_keys/ship-orders", "echo x > /etc/shiplord/apps/orders.json",
                       "cat /etc/shiplord/apps/orders.json", "echo x > /usr/local/sbin/shiplord-host",
                       "echo x > /etc/sudoers.d/shiplord-orders", "ls /etc/shiplord/env/orders",
                       "ls /var/lib/shiplord/apps/orders"):
            with self.subTest(script=script):
                self.assertNotEqual(as_account(script).returncode, 0)
        self.assertNotEqual(as_account("sudo -n /bin/true").returncode, 0)
        self.assertNotEqual(as_account("sudo -n /usr/local/sbin/shiplord-host --extra").returncode, 0)
        self.assertNotEqual(as_account("sudo -n /usr/local/sbin/shiplord-admin x").returncode, 0)

    def test_zz_deprovision_removes_access_and_leaves_a_clean_account_database(self):
        result = self.admin("deprovision", "orders", {})
        self.assertTrue(result["removed"]["account"] and result["removed"]["registration"])
        self.assertEqual(subprocess.run(["getent", "passwd", "ship-orders"], capture_output=True).returncode, 2)
        login = self.login("/usr/local/libexec/shiplord-dispatch", stdin=capabilities_frame("orders"))
        self.assertEqual(login.returncode, 255)
        self.assertFalse(Path("/etc/sudoers.d/shiplord-orders").exists())


if __name__ == "__main__":
    unittest.main()
