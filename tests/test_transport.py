from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord.cli import app
from shiplord.protocol import IncompatibleProtocol, ProtocolError, validate_request
from shiplord.servers import ServerError, load_server
from shiplord.transport import TransportError, probe_capabilities

FAKE_SSH = r'''import json, os, sys, time
mode = os.environ.get("FAKE_SSH_MODE", "ok")
if sys.argv[-1] != "/usr/local/libexec/shiplord-dispatch":
    sys.exit(90)
if mode == "early":
    sys.exit(1)
raw = sys.stdin.buffer.read()
request = json.loads(raw[4:])
response = {"status": "ok", "request_id": request["request_id"], "version": 1,
            "helper_version": "0.1.0", "actions": ["capabilities"]}
if mode == "wrong-id": response["request_id"] = "wrong"
if mode == "wrong-version": response["version"] = 2
if mode == "unsupported": response["actions"] = ["status"]
if mode in ("incompatible", "incompatible-success"):
    response = {"status": "incompatible", "request_id": request["request_id"], "supported_versions": [2]}
if mode == "stderr": os.write(2, b"s" * 2000000)
if mode == "hang": time.sleep(5)
body = json.dumps(response).encode()
out = len(body).to_bytes(4, "big") + body
if mode == "oversize": out = (1048577).to_bytes(4, "big")
if mode == "truncated": out = out[:-1]
if mode == "trailing": out += b"unexpected"
if mode == "fragmented":
    for byte in out:
        os.write(1, bytes([byte]))
else:
    os.write(1, out)
if mode == "wait-after":
    os.close(1)
    time.sleep(5)
if mode == "exit-failure": sys.exit(1)
if mode == "incompatible": sys.exit(4)
'''


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        directory = self.root / "bin"
        directory.mkdir()
        fake = directory / "fake.py"
        fake.write_text(FAKE_SSH)
        ssh = directory / "ssh"
        ssh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
        ssh.chmod(0o700)
        self.env = {"PATH": str(directory), "XDG_CONFIG_HOME": str(self.root / "config")}

    def make_alias(self, data=None):
        path = self.root / "config/shiplord/servers"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (path.parent).chmod(0o700)
        target = path / "production.json"
        target.write_text(data if data is not None else json.dumps({"schema_version": 1, "alias": "production", "ssh": "appuser@example.com"}))
        target.chmod(0o600)
        return target

    def test_capabilities_exchange_handles_fragmented_output_and_stderr_flood(self):
        for mode in ("fragmented", "stderr"):
            with self.subTest(mode=mode), patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE=mode)):
                response = probe_capabilities("appuser@example.com", timeout=3)
                self.assertEqual(response["actions"], ["capabilities"])

    def test_invalid_exchange_is_rejected_without_echoing_output(self):
        for mode in ("early", "wrong-id", "unsupported", "oversize", "truncated", "trailing", "exit-failure", "incompatible-success"):
            with self.subTest(mode=mode), patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE=mode)):
                with self.assertRaises(TransportError):
                    probe_capabilities("appuser@example.com", timeout=3)

    def test_incompatible_response_requires_the_documented_exit(self):
        for mode in ("incompatible",):
            with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE=mode)):
                with self.assertRaises(IncompatibleProtocol):
                    probe_capabilities("appuser@example.com", timeout=3)
        # A success response reporting another version cannot claim exit 4.
        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="wrong-version")):
            with self.assertRaises(TransportError):
                probe_capabilities("appuser@example.com", timeout=3)

    def test_hanging_peer_is_terminated_with_a_finite_deadline(self):
        for mode in ("hang", "wait-after"):
            with self.subTest(mode=mode), patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE=mode)):
                start = time.monotonic()
                with self.assertRaisesRegex(TransportError, "timed out"):
                    probe_capabilities("appuser@example.com", timeout=0.3)
                self.assertLess(time.monotonic() - start, 3)

    def test_zero_deadline_and_pipe_failures_do_not_hang(self):
        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="early")):
            with self.assertRaisesRegex(TransportError, "timed out"):
                probe_capabilities("appuser@example.com", timeout=0)
            with patch("shiplord.transport.os.set_blocking", side_effect=OSError("pipe error")):
                with self.assertRaises(TransportError):
                    probe_capabilities("appuser@example.com", timeout=3)
            with patch("shiplord.transport.os.write", return_value=0):
                with self.assertRaisesRegex(TransportError, "closed"):
                    probe_capabilities("appuser@example.com", timeout=3)

    def test_interrupted_nonblocking_read_is_retried(self):
        original_read = os.read
        interrupted = False

        def read(fd, size):
            nonlocal interrupted
            if size == 65536 and not interrupted:
                interrupted = True
                raise BlockingIOError()
            return original_read(fd, size)

        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="ok")), patch("shiplord.transport.os.read", side_effect=read):
            self.assertEqual(probe_capabilities("appuser@example.com", timeout=3)["status"], "ok")
        self.assertTrue(interrupted)

    def test_deadline_expiry_after_eof_still_fails(self):
        original_read = os.read
        original_clock = time.monotonic
        received_eof = False

        def read(fd, size):
            nonlocal received_eof
            data = original_read(fd, size)
            if size == 65536 and not data:
                received_eof = True
            return data

        def clock():
            return original_clock() + (30 if received_eof else 0)

        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="ok")), patch("shiplord.transport.os.read", side_effect=read), patch("shiplord.transport.time.monotonic", side_effect=clock):
            with self.assertRaisesRegex(TransportError, "timed out"):
                probe_capabilities("appuser@example.com", timeout=3)
        self.assertTrue(received_eof)

    def test_cleanup_tolerates_an_already_exited_process(self):
        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="early")), patch("shiplord.transport.os.killpg", side_effect=ProcessLookupError), patch.object(subprocess.Popen, "kill", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(TransportError, "timed out"):
                probe_capabilities("appuser@example.com", timeout=0)

    def test_cleanup_failure_is_reported_when_signals_are_denied(self):
        process_type = subprocess.Popen
        spawned = []

        def start(*args, **kwargs):
            process = process_type(*args, **kwargs)
            spawned.append(process)
            return process

        try:
            with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="hang")), patch("shiplord.transport.os.killpg", side_effect=PermissionError), patch.object(process_type, "kill", side_effect=PermissionError), patch("shiplord.transport.subprocess.Popen", side_effect=start):
                with self.assertRaisesRegex(TransportError, "could not stop"):
                    probe_capabilities("appuser@example.com", timeout=0.2)
        finally:
            for process in spawned:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)

    def test_failed_spawn_is_reported(self):
        with patch("subprocess.Popen", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(TransportError, "could not start SSH"):
                probe_capabilities("appuser@example.com")

    def test_doctor_uses_registered_alias_without_claiming_readiness(self):
        self.make_alias()
        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="ok")):
            result = CliRunner().invoke(app, ["doctor", "--server", "production", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("server readiness", json.loads(result.stdout)["scope"])

    def test_doctor_reports_incompatible_version_and_bad_alias(self):
        self.make_alias()
        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="incompatible")):
            result = CliRunner().invoke(app, ["doctor", "--server", "production", "--json"])
        self.assertEqual(result.exit_code, 4)
        self.assertIn("incompatible", json.loads(result.stdout)["error"])
        with patch.dict(os.environ, self.env):
            result = CliRunner().invoke(app, ["doctor", "--server", "--bad", "--json"])
        self.assertEqual(result.exit_code, 2)

    def test_doctor_reports_transport_failure_as_json(self):
        self.make_alias()
        with patch.dict(os.environ, dict(self.env, FAKE_SSH_MODE="early")):
            result = CliRunner().invoke(app, ["doctor", "--server", "production", "--json"])
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_missing_alias_does_not_create_directories_or_start_ssh(self):
        with patch.dict(os.environ, self.env), patch("subprocess.Popen") as popen:
            result = CliRunner().invoke(app, ["doctor", "--server", "production", "--json"])
        self.assertEqual(result.exit_code, 3)
        popen.assert_not_called()
        self.assertFalse((self.root / "config").exists())

    def test_alias_reader_rejects_corruption_and_unsafe_file_modes(self):
        invalid = ['{"schema_version":1,"alias":"production","ssh":"--bad"}',
                   '{"schema_version":1,"schema_version":1,"alias":"production","ssh":"appuser@example.com"}',
                   '{"schema_version":true,"alias":"production","ssh":"appuser@example.com"}',
                   '{"schema_version":1,"alias":"other","ssh":"appuser@example.com"}',
                   'x' * 4097, '\ufffd']
        for value in invalid:
            with self.subTest(value=value[:50]):
                self.make_alias(value)
                with patch.dict(os.environ, self.env), self.assertRaises(ServerError):
                    load_server("production")
        path = self.make_alias()
        path.chmod(0o644)
        with patch.dict(os.environ, self.env), self.assertRaises(ServerError):
            load_server("production")

    def test_alias_growth_during_read_is_rejected(self):
        self.make_alias("x" * 4097)
        original_fstat = os.fstat

        def stale_stat(fd):
            details = original_fstat(fd)
            if __import__("stat").S_ISREG(details.st_mode):
                return types.SimpleNamespace(st_mode=details.st_mode, st_uid=details.st_uid, st_size=0)
            return details

        with patch.dict(os.environ, self.env), patch("shiplord.servers.os.fstat", side_effect=stale_stat):
            with self.assertRaises(ServerError):
                load_server("production")

    def test_doctor_without_mode_reports_unimplemented_app_check(self):
        result = CliRunner().invoke(app, ["doctor", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not implemented", json.loads(result.stdout)["error"])

    def test_server_capabilities_is_the_only_request_without_an_application(self):
        base = {"version": 1, "request_id": "request-1", "action": "capabilities", "application_id": None,
                "commit": None, "arguments": {}}
        self.assertIsNone(validate_request(base)["application_id"])
        with self.assertRaises(ProtocolError):
            validate_request(dict(base, action="deploy"))
