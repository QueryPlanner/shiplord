from __future__ import annotations

import io
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shiplord.authorization import AuthorizationError, authorize_application
from shiplord.framing import read_frame, write_frame
from shiplord.privileged_host import serve


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "etc/shiplord/apps"
        self.root.mkdir(parents=True)
        self.uid = os.getuid()
        self.user = pwd.getpwuid(self.uid).pw_name
        self.registration = {"schema_version": 1, "application_id": "orders",
                             "deployment_uid": self.uid, "deployment_user": self.user, "runtime": "docker",
                             "api_domain": "orders.example.com", "host_port": 20000,
                             "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096}
        self.file = self.root / "orders.json"
        self.file.write_text(json.dumps(self.registration))
        self.environment = {"SUDO_UID": str(self.uid), "SUDO_USER": self.user}
        self.real_fstat = os.fstat

    def trusted_stat(self, fd):
        info = self.real_fstat(fd)
        return types.SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022, st_size=info.st_size)

    @contextmanager
    def checks(self):
        with patch("shiplord.authorization.REGISTRATION_ROOT", self.root), patch("shiplord.authorization.os.geteuid", return_value=0), patch("shiplord.authorization.os.fstat", side_effect=self.trusted_stat), patch.dict(os.environ, self.environment):
            yield

    def authorize(self, application="orders"):
        with self.checks():
            return authorize_application(application)

    def test_authorization_returns_the_registration_for_its_app(self):
        self.assertEqual(self.authorize(), self.registration)

    def test_matching_account_and_registration_allow_only_its_app(self):
        self.authorize()
        other = dict(self.registration, application_id="other", deployment_uid=self.uid + 1)
        (self.root / "other.json").write_text(json.dumps(other))
        with self.assertRaises(AuthorizationError):
            self.authorize("other")

    def test_nonroot_invalid_app_and_missing_or_mismatched_identity_are_denied(self):
        with patch("shiplord.authorization.os.geteuid", return_value=123):
            with self.assertRaises(AuthorizationError):
                authorize_application("orders")
        for app in (None, "../orders", "-orders", "x" * 49):
            with self.subTest(app=app), patch("shiplord.authorization.os.geteuid", return_value=0):
                with self.assertRaises(AuthorizationError):
                    authorize_application(app)
        for env in ({"SUDO_UID": "0", "SUDO_USER": self.user}, {"SUDO_UID": str(self.uid), "SUDO_USER": "wrong"},
                    {"SUDO_UID": "9999999999", "SUDO_USER": self.user}, {"SUDO_UID": str(self.uid), "SUDO_USER": ""}):
            with self.subTest(env=env), patch("shiplord.authorization.os.geteuid", return_value=0), patch.dict(os.environ, env):
                with self.assertRaises(AuthorizationError):
                    authorize_application("orders")

    def test_malformed_or_inconsistent_registrations_are_denied(self):
        cases = [dict(self.registration, schema_version=True), dict(self.registration, application_id="different"),
                 dict(self.registration, deployment_uid=True), dict(self.registration, deployment_user="other"),
                 dict(self.registration, extra="unexpected"), [], "x" * 4097]
        for case in cases:
            with self.subTest(case=str(case)[:80]):
                self.file.write_text(json.dumps(case))
                with self.assertRaises(AuthorizationError):
                    self.authorize()
        self.file.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(AuthorizationError):
            self.authorize()

    def test_symlinked_registration_and_directory_are_denied(self):
        other = self.root / "other"
        other.write_text(json.dumps(self.registration))
        self.file.unlink()
        self.file.symlink_to(other)
        with self.assertRaises(AuthorizationError):
            self.authorize()
        link = self.root.parent / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with patch("shiplord.authorization.REGISTRATION_ROOT", link), patch("shiplord.authorization.os.geteuid", return_value=0), patch("shiplord.authorization.os.fstat", side_effect=self.trusted_stat), patch.dict(os.environ, self.environment):
            with self.assertRaises(AuthorizationError):
                authorize_application("orders")

    def test_untrusted_file_and_directory_metadata_are_denied(self):
        for kind in ("directory", "file"):
            def unsafe(fd):
                info = self.trusted_stat(fd)
                if (kind == "directory" and stat.S_ISDIR(info.st_mode)) or (kind == "file" and stat.S_ISREG(info.st_mode)):
                    info.st_mode |= 0o020
                return info
            with self.subTest(kind=kind), patch("shiplord.authorization.REGISTRATION_ROOT", self.root), patch("shiplord.authorization.os.geteuid", return_value=0), patch("shiplord.authorization.os.fstat", side_effect=unsafe), patch.dict(os.environ, self.environment):
                with self.assertRaises(AuthorizationError):
                    authorize_application("orders")

    def test_registration_growing_after_stat_is_denied(self):
        self.file.write_text("x" * 4097)
        def stale_stat(fd):
            info = self.trusted_stat(fd)
            if stat.S_ISREG(info.st_mode):
                info.st_size = 0
            return info
        with patch("shiplord.authorization.REGISTRATION_ROOT", self.root), patch("shiplord.authorization.os.geteuid", return_value=0), patch("shiplord.authorization.os.fstat", side_effect=stale_stat), patch.dict(os.environ, self.environment):
            with self.assertRaises(AuthorizationError):
                authorize_application("orders")

    def test_privileged_helper_checks_binding_before_disclosing_capabilities(self):
        request = {"version": 1, "request_id": "job-1", "action": "capabilities",
                   "application_id": "orders", "commit": None, "arguments": {}}
        encoded = io.BytesIO()
        write_frame(encoded, request)
        output = io.BytesIO()
        with self.checks():
            self.assertEqual(serve(io.BytesIO(encoded.getvalue()), output), 0)
        self.assertEqual(read_frame(io.BytesIO(output.getvalue()))["actions"],
                         ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover", "docker_storage_v1", "docker_shutdown_v1", "docker_generic_v1"])
        request["application_id"] = "other"
        encoded = io.BytesIO()
        write_frame(encoded, request)
        output = io.BytesIO()
        with self.checks():
            self.assertEqual(serve(io.BytesIO(encoded.getvalue()), output), 3)
        self.assertEqual(read_frame(io.BytesIO(output.getvalue())), {"status": "error", "error": "access_denied"})

    def test_privileged_helper_rejects_mutations_bad_versions_and_arguments(self):
        base = {"version": 1, "request_id": "job-1", "action": "capabilities",
                "application_id": "orders", "commit": None, "arguments": {}}
        cases = [(dict(base, action="deploy"), 2, "invalid_request"),  # missing deployment arguments
                 (dict(base, action="status", arguments={"unexpected": 1}), 2, "invalid_request"),
                 (dict(base, action="logs", arguments={"tail": 0}), 2, "invalid_request"),
                 (dict(base, action="rollback", commit="a" * 40), 2, "invalid_request"),
                 (dict(base, version=2), 4, "incompatible"),
                 (dict(base, arguments={"unexpected": True}), 2, "invalid_request"),
                 (dict(base, commit="a" * 40), 2, "invalid_request")]
        for message, expected, status in cases:
            with self.subTest(message=message):
                encoded, output = io.BytesIO(), io.BytesIO()
                write_frame(encoded, message)
                with self.checks():
                    self.assertEqual(serve(io.BytesIO(encoded.getvalue()), output), expected)
                response = read_frame(io.BytesIO(output.getvalue()))
                self.assertEqual(response.get("error", response["status"]), status)

    def test_privileged_helper_handles_malformed_input_and_output_failure(self):
        output = io.BytesIO()
        self.assertEqual(serve(io.BytesIO(b"bad"), output), 2)
        self.assertEqual(read_frame(io.BytesIO(output.getvalue()))["error"], "invalid_request")
        valid = {"version": 1, "request_id": "job-1", "action": "capabilities",
                 "application_id": "orders", "commit": None, "arguments": {}}
        encoded, output = io.BytesIO(), io.BytesIO()
        write_frame(encoded, valid)
        self.assertEqual(serve(io.BytesIO(encoded.getvalue() + b"extra"), output), 2)
        self.assertEqual(read_frame(io.BytesIO(output.getvalue()))["error"], "invalid_request")
        class BrokenWriter:
            def write(self, data):
                raise OSError("failure")
        self.assertEqual(serve(io.BytesIO(b"bad"), BrokenWriter()), 1)

    def test_direct_nonroot_process_cannot_use_the_privileged_helper(self):
        request = {"version": 1, "request_id": "job-1", "action": "capabilities",
                   "application_id": "orders", "commit": None, "arguments": {}}
        body = json.dumps(request).encode()
        result = subprocess.run([sys.executable, "-m", "shiplord.privileged_host"], input=len(body).to_bytes(4, "big") + body,
                                capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(read_frame(io.BytesIO(result.stdout))["error"], "access_denied")
        extra = subprocess.run([sys.executable, "-m", "shiplord.privileged_host", "unexpected"],
                               capture_output=True, timeout=30, check=False)
        self.assertEqual(extra.returncode, 2)
