from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord.cli import app
from shiplord.servers import ServerError, SSHCheckError, register_server


class ServerRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$SSH_TEST_ARGS"\nexit "${SSH_TEST_EXIT:-0}"\n')
        ssh.chmod(0o700)
        self.env = {"XDG_CONFIG_HOME": str(self.root / "config"), "PATH": str(self.bin),
                    "SSH_TEST_ARGS": str(self.root / "arguments")}

    def invoke(self, *args):
        with patch.dict(os.environ, self.env):
            return CliRunner().invoke(app, ["server", "add", *args])

    def test_registration_uses_strict_fresh_ssh_and_private_storage(self):
        result = self.invoke("production", "--ssh", "appuser@ubuntu-4gb-hel1-1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("CI host-key setup are not implemented", result.output)
        options = (self.root / "arguments").read_text().splitlines()
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "VerifyHostKeyDNS=no",
                       "UpdateHostKeys=no", "ControlMaster=no", "ControlPath=none",
                       "ClearAllForwardings=yes", "ForwardAgent=no", "RequestTTY=no", "ConnectTimeout=10"):
            self.assertIn(option, options)
        self.assertEqual(options[-3:], ["--", "appuser@ubuntu-4gb-hel1-1", "true"])
        servers = self.root / "config/shiplord/servers"
        saved = servers / "production.json"
        self.assertEqual(json.loads(saved.read_text()), {"schema_version": 1, "alias": "production", "ssh": "appuser@ubuntu-4gb-hel1-1"})
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(servers.stat().st_mode & 0o777, 0o700)
        self.assertEqual([item.name for item in servers.iterdir()], ["production.json"])

    def test_invalid_target_never_invokes_ssh(self):
        targets = ["-option", "user@-option", "user@host;touch", "user@host name", "user@a@b", "user@host\n", "--user@example.com", "user@fe80::1%en0"]
        for target in targets:
            with self.subTest(target=target):
                result = self.invoke("production", "--ssh", target)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertFalse((self.root / "arguments").exists())

    def test_ipv4_and_ipv6_targets_are_passed_as_single_arguments(self):
        for alias, target in (("ipv4", "appuser@127.0.0.1"), ("ipv6", "appuser@::1")):
            with self.subTest(target=target):
                result = self.invoke(alias, "--ssh", target)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual((self.root / "arguments").read_text().splitlines()[-2], target)

    def test_failed_ssh_does_not_register_alias(self):
        with patch.dict(os.environ, dict(self.env, SSH_TEST_EXIT="255")):
            result = CliRunner().invoke(app, ["server", "add", "production", "--ssh", "appuser@example.com"])
        self.assertEqual(result.exit_code, 3)
        self.assertFalse((self.root / "config/shiplord/servers/production.json").exists())

    def test_duplicate_alias_preserves_original(self):
        first = self.invoke("production", "--ssh", "appuser@example.com")
        second = self.invoke("production", "--ssh", "other@example.com")
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 1, second.output)
        saved = self.root / "config/shiplord/servers/production.json"
        self.assertEqual(json.loads(saved.read_text())["ssh"], "appuser@example.com")
        self.assertEqual([item.name for item in saved.parent.iterdir()], ["production.json"])

    def test_ssh_timeout_and_missing_executable_are_reported(self):
        for error in (subprocess.TimeoutExpired("ssh", 15), FileNotFoundError()):
            with self.subTest(error=type(error).__name__), patch("subprocess.run", side_effect=error):
                with self.assertRaises(SSHCheckError):
                    register_server("production", "appuser@example.com")
        self.assertFalse((self.root / "config").exists())

    def test_untrusted_config_paths_do_not_receive_aliases(self):
        config = self.root / "config"
        config.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (config / "shiplord").symlink_to(outside, target_is_directory=True)
        result = self.invoke("production", "--ssh", "appuser@example.com")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(list(outside.iterdir()), [])

    def test_insecure_directory_permissions_are_rejected(self):
        directory = self.root / "config/shiplord"
        directory.mkdir(parents=True, mode=0o755)
        directory.chmod(0o755)
        result = self.invoke("production", "--ssh", "appuser@example.com")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertFalse((directory / "servers/production.json").exists())

    def test_relative_config_path_is_rejected(self):
        with patch.dict(os.environ, dict(self.env, XDG_CONFIG_HOME="relative")):
            result = CliRunner().invoke(app, ["server", "add", "production", "--ssh", "appuser@example.com"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("absolute path", result.output)

    def test_failed_write_does_not_publish_an_alias(self):
        with patch.dict(os.environ, self.env), patch("shiplord.servers.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(ServerError, "could not save"):
                register_server("production", "appuser@example.com")
        directory = self.root / "config/shiplord/servers"
        self.assertEqual(list(directory.iterdir()), [])
