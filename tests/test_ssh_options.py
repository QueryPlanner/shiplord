from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shiplord import cli
from shiplord.client import HelperClient
from shiplord.servers import InvalidTarget, ServerError, SshOptions, ssh_command


class SshOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.key, self.hosts = self.directory / "key", self.directory / "known_hosts"
        self.key.write_text("k")
        self.hosts.write_text("h")

    def test_environment_values_are_read_and_validated(self):
        options = SshOptions.from_environment({"SHIPLORD_SSH_IDENTITY": str(self.key),
                                               "SHIPLORD_SSH_KNOWN_HOSTS_FILE": str(self.hosts), "SHIPLORD_SSH_PORT": "2222"})
        self.assertEqual((options.identity, options.known_hosts, options.port), (self.key, self.hosts, 2222))
        self.assertEqual(SshOptions.from_environment({}), SshOptions())
        empty = {"SHIPLORD_SSH_IDENTITY": "", "SHIPLORD_SSH_KNOWN_HOSTS_FILE": "", "SHIPLORD_SSH_PORT": ""}
        self.assertEqual(SshOptions.from_environment(empty), SshOptions())  # an unset GitHub variable expands to ""

    def test_bad_values_are_rejected_without_echoing_them(self):
        bad = [{"SHIPLORD_SSH_IDENTITY": "relative/key"}, {"SHIPLORD_SSH_IDENTITY": str(self.directory / "missing")},
               {"SHIPLORD_SSH_KNOWN_HOSTS_FILE": str(self.directory)}, {"SHIPLORD_SSH_PORT": "0"},
               {"SHIPLORD_SSH_PORT": "65536"}, {"SHIPLORD_SSH_PORT": "22; rm -rf /"}, {"SHIPLORD_SSH_PORT": "-1"}]
        for environ in bad:
            with self.subTest(environ=environ), self.assertRaises(ServerError) as caught:
                SshOptions.from_environment(environ)
            for value in environ.values():
                self.assertNotIn(value, str(caught.exception))

    def test_a_pinned_known_hosts_file_is_the_only_trust_source_and_checking_stays_strict(self):
        command = ssh_command("ship-orders@host.example.com", "remote", SshOptions(self.key, self.hosts, 2222))
        for expected in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes",
                         f"UserKnownHostsFile={self.hosts}", "GlobalKnownHostsFile=/dev/null"):
            self.assertIn(expected, command)
        self.assertEqual(command[command.index("-F") + 1], "/dev/null")  # hermetic: no ~/.ssh/config behind the pins
        self.assertEqual(command[command.index("-i") + 1], str(self.key))
        self.assertEqual(command[command.index("-p") + 1], "2222")
        self.assertEqual(command[-3:], ["--", "ship-orders@host.example.com", "remote"])

    def test_the_users_ssh_config_is_ignored_only_when_both_the_identity_and_the_host_keys_are_pinned(self):
        self.assertNotIn("-F", ssh_command("u@h", "r", SshOptions(identity=self.key)))
        self.assertNotIn("-F", ssh_command("u@h", "r", SshOptions(known_hosts=self.hosts)))
        self.assertNotIn("-F", ssh_command("u@h", "r", SshOptions(port=2222)))
        self.assertIn("-F", ssh_command("u@h", "r", SshOptions(self.key, self.hosts)))

    def test_without_options_the_command_is_unchanged(self):
        plain = ssh_command("ship-orders@host", "remote")
        for flag in ("-i", "-p"):
            self.assertNotIn(flag, plain)
        self.assertFalse(any(part.startswith(("UserKnownHostsFile", "IdentitiesOnly")) for part in plain))
        self.assertEqual(plain, ssh_command("ship-orders@host", "remote", SshOptions()))

    def test_the_target_is_validated_whatever_the_options(self):
        for target in ("-oProxyCommand=evil@host", "a@b c", "user@host;id", "nohost", "u@-host"):
            with self.subTest(target=target), self.assertRaises(InvalidTarget):
                ssh_command(target, "remote", SshOptions(self.key, self.hosts))

    def test_a_helper_client_uses_an_explicit_deployment_target_as_given(self):
        derived = HelperClient("orders", "appuser@server.example.com")
        explicit = HelperClient("orders", "ship-orders@server.example.com", explicit_target=True)
        self.assertEqual(derived.target, "ship-orders@server.example.com")
        self.assertEqual(explicit.target, "ship-orders@server.example.com")
        other = HelperClient("orders", "someone@server.example.com", explicit_target=True)
        self.assertEqual(other.target, "someone@server.example.com")  # not rewritten: the caller named the account

    def test_the_cli_builds_its_client_from_ci_environment_settings(self):
        manifest = {"name": "orders", "server": "production"}
        environ = {"SHIPLORD_SSH_TARGET": "ship-orders@server.example.com", "SHIPLORD_SSH_IDENTITY": str(self.key),
                   "SHIPLORD_SSH_KNOWN_HOSTS_FILE": str(self.hosts)}
        with patch.dict(os.environ, environ, clear=False):
            client = cli._helper(manifest)
        self.assertEqual(client.target, "ship-orders@server.example.com")
        self.assertEqual((client.ssh_options.identity, client.ssh_options.known_hosts), (self.key, self.hosts))
        with patch.dict(os.environ, dict(environ, SHIPLORD_SSH_TARGET="bad target"), clear=False):
            with self.assertRaises(InvalidTarget):
                cli._helper(manifest)

    def test_calls_carry_the_options_into_the_ssh_command(self):
        seen = []

        def runner(command, payload, timeout):
            seen.append(command)
            return b"", b"", 1
        client = HelperClient("orders", "ship-orders@h.example.com", runner=runner, explicit_target=True,
                              ssh_options=SshOptions(self.key, self.hosts))
        with self.assertRaises(Exception):
            client.call("status")
        self.assertIn(f"UserKnownHostsFile={self.hosts}", seen[0])


if __name__ == "__main__":
    unittest.main()
