from __future__ import annotations

import hashlib
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from shiplord.layout import Layout, runtime_user  # noqa: E402
from shiplord.native import (ARCHITECTURES, JOURNALCTL, SYSTEMCTL, NativeRuntime, elf_architecture,  # noqa: E402
                             host_architecture, render_environment, render_unit, systemd_quote, verify_elf)
from shiplord.runtime import RuntimeFailure, release_id  # noqa: E402
from shiplord.system import CommandError, CommandResult  # noqa: E402

REGISTRATION = {"application_id": "orders", "host_port": 20007, "api_domain": "orders.example.com"}


def elf(machine=0x3E, file_type=3, klass=2, data=1, body=b"\0" * 100) -> bytes:
    ident = b"\x7fELF" + bytes([klass, data, 1, 0]) + b"\0" * 8
    return ident + struct.pack("<HH", file_type, machine) + body


def release(binary: bytes, sequence=1000, origins=(), memory=256):
    return {"commit": "a" * 40, "sequence": sequence,
            "artifact": {"kind": "native", "sha256": hashlib.sha256(binary).hexdigest(), "size": len(binary),
                         "architecture": "linux-amd64"},
            "configuration": {"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk",
                              "runtime": "native", "http": {"api_domain": "orders.example.com",
                              "frontend_origins": list(origins), "container_port": 8080, "readiness_path": "/ready"},
                              "resources": {"memory_mb": memory},
                              "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}}


def systemd_unquote(text: str) -> str:
    """Reference reader for the double-quoted form systemd uses in EnvironmentFile values."""
    assert text[0] == text[-1] == '"'
    out, index, body = [], 0, text[1:-1]
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 1
            out.append({"n": "\n", "t": "\t"}.get(body[index], body[index]))
        else:
            assert char != '"', "unescaped quote would end the value early"
            out.append(char)
        index += 1
    return "".join(out)


class ElfTests(unittest.TestCase):
    def test_accepts_executables_and_pie_for_x86_64(self):
        verify_elf(elf(file_type=2))
        verify_elf(elf(file_type=3))

    def test_rejects_everything_else_with_an_actionable_reason(self):
        cases = {
            "macho": (b"\xcf\xfa\xed\xfe" + b"\0" * 64, "not a Linux ELF"),
            "script": (b"#!/bin/sh\necho hi\n" + b"\0" * 64, "not a Linux ELF"),
            "short": (b"\x7fELF", "not a Linux ELF"),
            "32-bit": (elf(klass=1), "64-bit"),
            "big-endian": (elf(data=2), "64-bit little-endian"),
            "arm64": (elf(machine=0xB7), "another CPU architecture"),
            "relocatable": (elf(file_type=1), "not an executable"),
            "core": (elf(file_type=4), "not an executable"),
        }
        for label, (header, expected) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(RuntimeFailure, expected):
                verify_elf(header)


class ArchitectureTests(unittest.TestCase):
    def test_the_architecture_is_read_from_the_elf_header_for_both_supported_cpus(self):
        self.assertEqual(elf_architecture(elf(machine=0x3E)), "linux-amd64")
        self.assertEqual(elf_architecture(elf(machine=0xB7)), "linux-arm64")
        verify_elf(elf(machine=0xB7), "linux-arm64")
        with self.assertRaisesRegex(RuntimeFailure, "does not support"):
            elf_architecture(elf(machine=0x28))  # 32-bit ARM machine value in a 64-bit header
        with self.assertRaises(RuntimeFailure):
            elf_architecture(b"#!/bin/sh")

    def test_the_host_architecture_follows_the_machine_name(self):
        for machine, expected in (("x86_64", "linux-amd64"), ("AMD64", "linux-amd64"), ("aarch64", "linux-arm64"),
                                  ("arm64", "linux-arm64")):
            with self.subTest(machine=machine), patch("platform.machine", return_value=machine):
                self.assertEqual(host_architecture(), expected)
        self.assertEqual(set(ARCHITECTURES), {"linux-amd64", "linux-arm64"})


class RenderingTests(unittest.TestCase):
    def test_quoting_round_trips_every_awkward_value_under_systemd_rules(self):
        values = ['plain', 'with space', 'a"quote', "it's", "back\\slash", "$HOME ${X} %h", "# not a comment", "a=b=c",
                  "tab\there", "unicode-é-日本", "trailing\\", '"', "\\\"", ""]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(systemd_unquote(systemd_quote(value)), value)

    def test_managed_values_win_and_origins_are_json(self):
        text = render_environment(REGISTRATION, release(elf(), origins=["https://app.example.com"]),
                                  {"DATABASE_URL": "postgres://u:p@h/d?x=1", "TOKEN": 'a"b'})
        lines = dict(line.split("=", 1) for line in text.splitlines())
        decoded = {key: systemd_unquote(value) for key, value in lines.items()}
        self.assertEqual(decoded["HOST"], "127.0.0.1")
        self.assertEqual(decoded["PORT"], "20007")
        self.assertEqual(decoded["ALLOW_ORIGINS"], '["https://app.example.com"]')
        self.assertEqual((decoded["SERVE_WEB_INTERFACE"], decoded["RELOAD_AGENTS"], decoded["AGENT_NAME"]),
                         ("false", "false", "orders"))
        self.assertEqual((decoded["DATABASE_URL"], decoded["TOKEN"]), ("postgres://u:p@h/d?x=1", 'a"b'))
        collision = render_environment(REGISTRATION, release(elf()), {"PORT": "1"})
        self.assertIn('PORT="20007"', collision)

    def test_the_unit_is_confined_limited_and_bound_to_one_release(self):
        layout = Layout(Path("/"))
        binary = elf()
        unit = render_unit(layout, REGISTRATION, release(binary))
        rid = release_id(release(binary))
        for expected in (f"# shiplord-release: {rid}", "User=app-orders", "Group=app-orders",
                         f"ExecStart=/opt/shiplord/orders/{rid}/app", "EnvironmentFile=/etc/shiplord/env/orders/native.env",
                         "MemoryMax=256M", "MemorySwapMax=0", "Restart=always", "NoNewPrivileges=yes",
                         "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "CapabilityBoundingSet=",
                         "SocketBindDeny=any", f"SocketBindAllow=tcp:{REGISTRATION['host_port']}",
                         "ReadWritePaths=/var/lib/shiplord/data/orders", "WorkingDirectory=/var/lib/shiplord/data/orders",
                         "WantedBy=multi-user.target"):
            self.assertIn(expected, unit.splitlines())
        self.assertEqual(sum(1 for line in unit.splitlines() if line.startswith("ExecStart=")), 1)
        self.assertNotIn("User=root", unit)

    def test_runtime_account_names_are_short_distinct_and_disjoint_from_deployment_accounts(self):
        self.assertEqual(runtime_user("orders"), "app-orders")
        long_a, long_b = "a" * 40, "a" * 39 + "b"
        self.assertNotEqual(runtime_user(long_a), runtime_user(long_b))
        for name in (runtime_user(long_a), runtime_user(long_b)):
            self.assertLessEqual(len(name), 32)
        from shiplord.layout import deployment_user
        self.assertNotEqual(runtime_user("orders"), deployment_user("orders"))


class FakeSystem:
    def __init__(self):
        self.calls, self.results, self.errors = [], {}, {}

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        self.calls.append(list(argv))
        key = argv[1] if argv[0] == SYSTEMCTL else argv[0]
        if key in self.errors:
            raise self.errors[key]
        return self.results.get(key, CommandResult(0, ""))

    def systemctl(self):
        return [call[1:] for call in self.calls if call[0] == SYSTEMCTL]


class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch("shiplord.native.host_architecture", return_value="linux-amd64")  # not the machine running the tests
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        for directory in (self.layout.environments / "orders", self.layout.app_state / "orders" / "staging",
                          self.layout.systemd_units):
            directory.mkdir(parents=True)
        self.system = FakeSystem()
        self.runtime = NativeRuntime(self.system, self.layout)
        self.binary = elf()
        self.release = release(self.binary)

    def stage(self, data=None):
        path = self.runtime.staged_path(REGISTRATION, self.release)
        path.write_bytes(self.binary if data is None else data)
        return path

    def install(self):
        self.stage()
        self.runtime.prepare(REGISTRATION, self.release)


class PrepareTests(RuntimeTestCase):
    def test_installs_a_verified_binary_executable_and_removes_the_staged_copy(self):
        staged = self.stage()
        result = self.runtime.prepare(REGISTRATION, self.release)
        self.assertEqual(result, {"architecture": "linux-amd64", "installed": "new"})
        target = self.runtime.release_binary(REGISTRATION, self.release)
        self.assertEqual(target.read_bytes(), self.binary)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertFalse(staged.exists())
        self.assertEqual(os.listdir(target.parent), ["app"])
        self.assertEqual(self.system.calls, [])  # preparing never touches the running service

    def test_an_installed_release_is_not_reinstalled_and_needs_no_upload(self):
        self.install()
        self.assertEqual(self.runtime.prepare(REGISTRATION, self.release)["installed"], "already")

    def test_a_repeated_upload_of_an_installed_binary_is_forgotten_not_kept(self):
        self.install()
        staged = self.stage()
        self.assertEqual(self.runtime.prepare(REGISTRATION, self.release)["installed"], "already")
        self.assertFalse(staged.exists())
        staged = self.stage()
        self.runtime.forget_upload(REGISTRATION, self.release)
        self.assertFalse(staged.exists())
        self.runtime.forget_upload(REGISTRATION, self.release)  # idempotent

    def test_failures_are_prepare_failures_and_install_nothing(self):
        cases = {
            "not uploaded": (None, "not on the server"),
            "checksum mismatch": (elf(body=b"x" * 100), "checksum or size"),
            "wrong size": (self.binary + b"extra", "checksum or size"),
        }
        for label, (data, expected) in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.layout.releases / "orders", ignore_errors=True)
                if data is not None:
                    self.stage(data)
                with self.assertRaisesRegex(RuntimeFailure, expected) as caught:
                    self.runtime.prepare(REGISTRATION, self.release)
                self.assertEqual(caught.exception.phase, "prepare")
                self.assertFalse(self.runtime.release_binary(REGISTRATION, self.release).exists())

    def test_a_binary_for_another_architecture_than_the_server_is_refused_before_anything_is_installed(self):
        self.stage()
        with patch("shiplord.native.host_architecture", return_value="linux-arm64"):
            with self.assertRaisesRegex(RuntimeFailure, "runs linux-arm64 binaries, not linux-amd64"):
                self.runtime.prepare(REGISTRATION, self.release)
        self.assertFalse(self.runtime.release_binary(REGISTRATION, self.release).exists())

    def test_a_staged_non_elf_with_a_matching_checksum_is_still_refused(self):
        script = b"#!/bin/sh\necho owned\n" + b"\0" * 64
        self.release = release(script)
        self.stage(script)
        with self.assertRaisesRegex(RuntimeFailure, "not a Linux ELF"):
            self.runtime.prepare(REGISTRATION, self.release)
        self.assertFalse(self.runtime.release_binary(REGISTRATION, self.release).exists())


class StartStopTests(RuntimeTestCase):
    def test_start_writes_private_environment_and_unit_then_reloads_enables_and_restarts(self):
        self.install()
        (self.layout.environments / "orders" / "app.env").write_text("DATABASE_URL=postgres://x\n")
        (self.layout.environments / "orders" / "app.env").chmod(0o600)
        self.runtime.start(REGISTRATION, self.release)
        env = self.layout.environments / "orders" / "native.env"
        unit = self.runtime.unit_path(REGISTRATION)
        self.assertEqual((stat.S_IMODE(env.stat().st_mode), stat.S_IMODE(unit.stat().st_mode)), (0o600, 0o644))
        self.assertIn('DATABASE_URL="postgres://x"', env.read_text())
        self.assertEqual(self.system.systemctl(), [["daemon-reload"], ["enable", "shiplord-orders.service"],
                                                   ["reset-failed", "shiplord-orders.service"],
                                                   ["restart", "shiplord-orders.service"]])

    def test_an_unchanged_unit_skips_the_daemon_reload(self):
        self.install()
        self.runtime.start(REGISTRATION, self.release)
        self.system.calls.clear()
        self.runtime.start(REGISTRATION, self.release)
        self.assertEqual(self.system.systemctl(), [["enable", "shiplord-orders.service"],
                                                   ["reset-failed", "shiplord-orders.service"],
                                                   ["restart", "shiplord-orders.service"]])

    def test_start_refuses_a_missing_or_tampered_binary_and_a_bad_environment_before_any_systemctl(self):
        with self.assertRaisesRegex(RuntimeFailure, "missing or does not match"):
            self.runtime.start(REGISTRATION, self.release)
        self.install()
        self.runtime.release_binary(REGISTRATION, self.release).write_bytes(elf(body=b"y" * 100))
        with self.assertRaisesRegex(RuntimeFailure, "missing or does not match"):
            self.runtime.start(REGISTRATION, self.release)
        self.runtime.release_binary(REGISTRATION, self.release).write_bytes(self.binary)
        (self.layout.environments / "orders" / "app.env").write_text("PORT=1\n")
        (self.layout.environments / "orders" / "app.env").chmod(0o600)
        with self.assertRaisesRegex(RuntimeFailure, "environment file is unusable"):
            self.runtime.start(REGISTRATION, self.release)
        self.assertEqual(self.system.calls, [])
        self.assertFalse(self.runtime.unit_path(REGISTRATION).exists())

    def test_a_failed_reset_does_not_block_the_start_but_a_failed_restart_does(self):
        self.install()
        self.system.results["reset-failed"] = CommandResult(1, "Unit shiplord-orders.service not loaded.")
        self.runtime.start(REGISTRATION, self.release)  # tolerated: nothing was failed yet
        self.assertIn(["restart", "shiplord-orders.service"], self.system.systemctl())

    def test_systemctl_failures_are_start_failures_with_output(self):
        self.install()
        self.system.results["restart"] = CommandResult(1, "Job for shiplord-orders.service failed")
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.start(REGISTRATION, self.release)
        self.assertEqual((caught.exception.phase, "failed" in caught.exception.detail), ("start", True))
        self.system.errors["restart"] = CommandError("x", "timeout", "slow")
        with self.assertRaises(RuntimeFailure):
            self.runtime.start(REGISTRATION, self.release)

    def test_stop_is_a_no_op_without_a_unit_and_disables_and_stops_with_one(self):
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.system.calls, [])
        self.install()
        self.runtime.start(REGISTRATION, self.release)
        self.system.calls.clear()
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.system.systemctl(), [["disable", "--now", "shiplord-orders.service"]])
        self.system.results["disable"] = CommandResult(1, "busy")
        with self.assertRaises(RuntimeFailure):
            self.runtime.stop(REGISTRATION)


class InspectTests(RuntimeTestCase):
    def state(self, active, sub):
        self.system.results["show"] = CommandResult(0, f"ActiveState={active}\nSubState={sub}\n")

    def test_running_means_this_release_is_active_and_not_restarting(self):
        self.install()
        self.runtime.start(REGISTRATION, self.release)
        self.state("active", "running")
        self.assertTrue(self.runtime.is_running(REGISTRATION, self.release))
        for active, sub in (("activating", "auto-restart"), ("failed", "failed"), ("inactive", "dead"), ("active", "exited")):
            self.state(active, sub)
            self.assertFalse(self.runtime.is_running(REGISTRATION, self.release), (active, sub))
        self.state("active", "running")
        other = release(self.binary, sequence=2000)
        self.assertFalse(self.runtime.is_running(REGISTRATION, other))  # another release's unit is not "running"

    def test_no_unit_means_not_running_without_calling_systemctl(self):
        self.assertFalse(self.runtime.is_running(REGISTRATION, self.release))
        self.assertEqual(self.system.calls, [])

    def test_logs_come_from_the_journal_bounded_and_empty_on_failure(self):
        self.system.results[JOURNALCTL] = CommandResult(0, "line\n")
        self.assertEqual(self.runtime.logs(REGISTRATION, 10**9), "line\n")
        self.assertEqual(self.system.calls[0], [JOURNALCTL, "--unit", "shiplord-orders.service", "--lines", "1000",
                                               "--no-pager", "--output", "cat"])
        self.system.results[JOURNALCTL] = CommandResult(1, "no journal")
        self.assertEqual(self.runtime.logs(REGISTRATION, 5), "")
        self.system.errors[JOURNALCTL] = CommandError("x", "start")
        self.assertEqual(self.runtime.logs(REGISTRATION, 5), "")


class DiscardTests(RuntimeTestCase):
    def test_discard_removes_only_the_release_directory(self):
        self.install()
        target = self.runtime.release_binary(REGISTRATION, self.release).parent
        self.assertEqual(self.runtime.discard(REGISTRATION, self.release), [])
        self.assertFalse(target.exists())
        self.assertTrue((self.layout.releases / "orders").exists())

    def test_discard_also_removes_an_upload_that_was_never_installed(self):
        staged = self.stage()
        self.assertEqual(self.runtime.discard(REGISTRATION, self.release), [])
        self.assertFalse(staged.exists())

    def test_discard_refuses_a_symlinked_release_directory(self):
        target = self.runtime.release_binary(REGISTRATION, self.release).parent
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "precious").write_text("keep")
        target.parent.mkdir(parents=True)
        target.symlink_to(elsewhere)
        self.assertEqual(len(self.runtime.discard(REGISTRATION, self.release)), 1)
        self.assertTrue((elsewhere / "precious").exists())


if __name__ == "__main__":
    unittest.main()
