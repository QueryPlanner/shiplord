"""Disposable Ubuntu sshd integration. Enabled only on an isolated CI runner."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("SHIPLORD_TEST_SSHD") == "1", "requires disposable Ubuntu sshd runner")
class SSHIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise RuntimeError("The disposable sshd test requires Linux")
        cls.temp = tempfile.TemporaryDirectory(prefix="shiplord-ssh-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.root.chmod(0o711)
        cls.username = f"shiptest{os.getpid()}"
        subprocess.run(["sudo", "-n", "useradd", "-M", "-d", str(cls.root), "-s", "/bin/sh", cls.username], check=True)
        cls.addClassCleanup(cls._remove_user)
        # An invalid password hash unlocks public-key authentication without
        # enabling a usable password. Password authentication is disabled below.
        subprocess.run(["sudo", "-n", "usermod", "-p", "invalid-hash", cls.username], check=True)
        for name in ("host", "client", "wrong"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.root / name)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.authorized = cls.root / "authorized_keys"
        cls.authorized.write_text((cls.root / "client.pub").read_text())
        cls.authorized.chmod(0o644)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.known_hosts = cls.root / "known_hosts"
        cls.correct_key = f"[127.0.0.1]:{cls.port} " + (cls.root / "host.pub").read_text()
        cls.wrong_key = f"[127.0.0.1]:{cls.port} " + (cls.root / "wrong.pub").read_text()
        cls.known_hosts.write_text(cls.correct_key)
        cls.config = cls.root / "ssh_config"
        cls.config.write_text(
            f"Host fixture\n  HostName 127.0.0.1\n  Port {cls.port}\n  User {cls.username}\n"
            f"  IdentityFile {cls.root / 'client'}\n  IdentitiesOnly yes\n"
            f"  UserKnownHostsFile {cls.known_hosts}\n  GlobalKnownHostsFile /dev/null\n"
            f"  ControlMaster auto\n  ControlPersist 90\n  ControlPath {cls.root / 'control'}\n"
        )
        dispatcher = cls.root / "dispatcher"
        dispatcher.write_text(
            '#!/bin/sh\ncase "$SSH_ORIGINAL_COMMAND" in\n'
            '  true) exec /usr/bin/true ;;\n'
            f'  /usr/local/libexec/shiplord-dispatch) exec "{sys.executable}" -m shiplord.host ;;\n'
            '  *) exit 1 ;;\nesac\n'
        )
        dispatcher.chmod(0o755)
        daemon_config = cls.root / "sshd_config"
        daemon_config.write_text(
            f"Port {cls.port}\nListenAddress 127.0.0.1\nHostKey {cls.root / 'host'}\n"
            f"PidFile {cls.root / 'sshd.pid'}\nAuthorizedKeysFile {cls.authorized}\n"
            f"AllowUsers {cls.username}\nStrictModes no\nUsePAM no\nPubkeyAuthentication yes\n"
            f"ForceCommand {dispatcher}\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"
            "PermitUserEnvironment no\nLogLevel ERROR\n"
        )
        subprocess.run(["sudo", "-n", "mkdir", "-p", "/run/sshd"], check=True)
        subprocess.run(["sudo", "-n", "/usr/sbin/sshd", "-t", "-f", str(daemon_config)], check=True)
        cls.daemon = subprocess.Popen(["sudo", "-n", "/usr/sbin/sshd", "-D", "-e", "-f", str(daemon_config)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls._stop_daemon)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.daemon.poll() is not None:
                raise RuntimeError("Disposable sshd exited before it became ready")
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Disposable sshd did not become ready")
        directory = cls.root / "bin"
        directory.mkdir()
        wrapper = directory / "ssh"
        wrapper.write_text(f'#!/bin/sh\nexec /usr/bin/ssh -F "{cls.config}" "$@"\n')
        wrapper.chmod(0o700)
        cls.env = dict(os.environ, PATH=f"{directory}:{os.environ.get('PATH', '')}",
                       XDG_CONFIG_HOME=str(cls.root / "config"))
        cls.executable = str(Path(sys.executable).parent / "shiplord")
        cls.target = f"{cls.username}@fixture"

    @classmethod
    def _stop_daemon(cls):
        pidfile = cls.root / "sshd.pid"
        if pidfile.exists():
            pid = pidfile.read_text().strip()
            if pid.isdecimal():
                subprocess.run(["sudo", "-n", "kill", pid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            cls.daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.daemon.kill()
            cls.daemon.wait(timeout=5)

    @classmethod
    def _remove_user(cls):
        subprocess.run(["sudo", "-n", "userdel", cls.username], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_cli(self, alias):
        return subprocess.run([self.executable, "server", "add", alias, "--ssh", self.target],
                              env=self.env, capture_output=True, text=True, timeout=20, check=False)

    def run_ssh(self, *arguments):
        return subprocess.run(["/usr/bin/ssh", "-F", str(self.config), *arguments],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15, check=False)

    def assert_not_saved(self, alias):
        self.assertFalse((self.root / "config/shiplord/servers" / f"{alias}.json").exists())

    def test_verified_host_keys_authentication_and_control_master(self):
        self.assertEqual(self.run_cli("baseline").returncode, 0)
        doctor = subprocess.run([self.executable, "doctor", "--server", "baseline", "--json"],
                                env=self.env, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("protocol capabilities only", doctor.stdout)
        self.known_hosts.write_text("")
        self.assertEqual(self.run_cli("unknown").returncode, 3)
        self.assert_not_saved("unknown")

        self.known_hosts.write_text(self.correct_key)
        self.assertEqual(self.run_cli("baseline-two").returncode, 0)
        self.known_hosts.write_text(self.wrong_key)
        self.assertEqual(self.run_cli("changed").returncode, 3)
        self.assert_not_saved("changed")

        self.known_hosts.write_text(self.correct_key)
        self.assertEqual(self.run_cli("baseline-three").returncode, 0)
        self.authorized.write_text("")
        self.assertEqual(self.run_cli("revoked").returncode, 3)
        self.assert_not_saved("revoked")
        self.authorized.write_text((self.root / "client.pub").read_text())

        self.assertEqual(self.run_ssh("-MNf", "-o", "BatchMode=yes", self.target).returncode, 0)
        try:
            self.assertEqual(self.run_ssh("-O", "check", self.target).returncode, 0)
            self.known_hosts.write_text(self.wrong_key)
            self.assertEqual(self.run_ssh("-o", "BatchMode=yes", self.target, "true").returncode, 0)
            self.assertEqual(self.run_cli("master").returncode, 3)
            self.assert_not_saved("master")
        finally:
            self.run_ssh("-O", "exit", self.target)
