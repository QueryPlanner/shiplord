from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeCaddy, FakeProbes  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.native import NativeRuntime  # noqa: E402
from shiplord.privileged_host import serve  # noqa: E402
from shiplord.runtime import release_id  # noqa: E402
from shiplord.services import HostServices  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from shiplord.worker import run_job  # noqa: E402
from test_helper_actions import FakeLauncher, HelperTestCase  # noqa: E402
from test_native import FakeSystem, elf, release  # noqa: E402


def native_configuration(**http):
    value = release(elf())["configuration"]
    value["http"] = dict(value["http"], **http)
    return value


class NativeHelperTests(HelperTestCase):
    RUNTIME = "native"

    def setUp(self):
        super().setUp()
        for target in ("shiplord.native.host_architecture", "shiplord.privileged_host.host_architecture"):
            patcher = patch(target, return_value="linux-amd64")  # not the machine running the tests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.system = FakeSystem()
        self.system.results["show"] = CommandResult(0, "ActiveState=active\nSubState=running\n")
        self.native = NativeRuntime(self.system, self.layout)
        self.probes = type("P", (FakeProbes,), {"ready_once": lambda s, r, rel: True,
                                                "wait_ready": lambda s, r, rel, alive: {"attempts": 1, "ready": True}})(self.runtime)
        self.services = HostServices(self.layout, system=self.system, launcher=self.launcher, caddy=self.caddy,
                                     probes=self.probes, lock_timeout=0.3)
        self.binary = elf(body=os.urandom(4096))

    def frame_with(self, payload: bytes, binary=None, request_id="req-1", **overrides):
        binary = self.binary if binary is None else binary
        artifact = {"kind": "native", "sha256": hashlib.sha256(binary).hexdigest(), "size": len(binary),
                    "architecture": "linux-amd64"}
        arguments = {"sequence": 1000, "artifact": overrides.pop("artifact", artifact),
                     "configuration": overrides.pop("configuration", native_configuration())}
        message = {"version": 1, "request_id": request_id, "action": "deploy", "application_id": "orders",
                   "commit": "a" * 40, "arguments": arguments}
        buffer = io.BytesIO()
        write_frame(buffer, message)
        return buffer.getvalue() + payload

    def send(self, data: bytes):
        output = io.BytesIO()
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration), \
                patch("shiplord.privileged_host.extend_deadline") as extend:
            code = serve(io.BytesIO(data), output, lambda: self.services)
        return code, read_frame(io.BytesIO(output.getvalue())), extend

    def staging(self):
        directory = self.layout.app_state / "orders" / "staging"
        return sorted(os.listdir(directory)) if directory.exists() else []

    def test_a_native_deploy_stages_the_binary_creates_a_job_and_extends_the_deadline_by_size(self):
        code, response, extend = self.send(self.frame_with(self.binary))
        self.assertEqual((code, response["status"], response["job"]["state"]), (0, "accepted", "queued"))
        self.assertEqual(self.staging(), [hashlib.sha256(self.binary).hexdigest() + ".bin"])
        self.assertEqual(self.launcher.launched, [("orders", "req-1")])
        seconds = extend.call_args[0][0]
        self.assertTrue(30 < seconds < 60, seconds)  # a tiny file gets the base allowance only

    def test_the_worker_installs_runs_and_commits_the_release_through_the_native_runtime(self):
        self.send(self.frame_with(self.binary))
        self.services.runtime_factory = lambda services, registration: self.native
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        committed = self.state.read_state()["current"]
        self.assertEqual(committed["artifact"]["sha256"], hashlib.sha256(self.binary).hexdigest())
        installed = self.layout.releases / "orders" / release_id(committed) / "app"
        self.assertEqual(installed.read_bytes(), self.binary)
        self.assertEqual(self.staging(), [])  # the staged copy was consumed
        self.assertEqual([c[1] for c in self.system.calls if c[0].endswith("systemctl")][:4],
                         ["daemon-reload", "enable", "reset-failed", "restart"])
        unit = (self.layout.systemd_units / "shiplord-orders.service").read_text()
        self.assertIn(f"ExecStart={installed}", unit)

    def test_each_upload_violation_is_rejected_with_code_2_and_leaves_no_job_or_staging(self):
        good = self.frame_with(self.binary)
        header_length = len(good) - len(self.binary)
        cases = {
            "truncated upload": good[:-100],
            "extra bytes": good + b"x",
            "corrupted payload": good[:-1] + bytes([good[-1] ^ 1]),
            "not an ELF": self.frame_with(b"#!/bin/sh\n" + b"x" * 200, binary=b"#!/bin/sh\n" + b"x" * 200),
        }
        for label, data in cases.items():
            with self.subTest(label):
                code, response, _ = self.send(data)
                self.assertEqual((code, response["error"]), (2, "invalid_payload"), response)
                self.assertEqual(self.staging(), [])
                self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.launcher.launched, [])

    def test_invalid_declared_sizes_and_checksums_are_protocol_errors_before_reading_the_stream(self):
        for artifact in ({"kind": "native", "sha256": "0" * 64, "size": 0, "architecture": "linux-amd64"},
                         {"kind": "native", "sha256": "0" * 64, "size": 2**31, "architecture": "linux-amd64"},
                         {"kind": "native", "sha256": "0" * 64, "size": "10", "architecture": "linux-amd64"}):
            with self.subTest(size=artifact["size"]):
                code, response, _ = self.send(self.frame_with(b"payload", artifact=artifact))
                self.assertEqual(code, 2)
                self.assertEqual(self.staging(), [])

    def test_a_repeated_request_returns_its_job_and_drains_the_resent_upload(self):
        self.send(self.frame_with(self.binary))
        code, response, _ = self.send(self.frame_with(self.binary))
        self.assertEqual((code, response["status"]), (0, "accepted"))
        self.assertEqual(len(self.launcher.launched), 1)

    def test_a_refused_request_discards_the_upload_without_staging_it(self):
        bad = native_configuration(api_domain="evil.example.com")
        code, response, _ = self.send(self.frame_with(self.binary, configuration=bad))
        self.assertEqual((code, response["status"], response["outcome"]["status"]), (2, "finished", "rejected"))
        self.assertEqual(self.staging(), [])

    def test_a_stale_native_deploy_is_refused_before_its_upload_is_stored(self):
        self.send(self.frame_with(self.binary))
        self.services.runtime_factory = lambda services, registration: self.native
        run_job(self.services, "orders", "req-1")
        newer = os.urandom(512)
        stale = self.frame_with(elf(body=newer), binary=elf(body=newer), request_id="req-2")
        message = json.loads(stale[4:4 + struct.unpack(">I", stale[:4])[0]])
        message["arguments"]["sequence"] = 10  # older than the deployed release (sequence 1000)
        buffer = io.BytesIO()
        write_frame(buffer, message)
        code, response, _ = self.send(buffer.getvalue() + stale[4 + struct.unpack(">I", stale[:4])[0]:])
        self.assertEqual((code, response["status"], response["outcome"]["detail"]["error"]), (1, "finished", "stale_release"))
        self.assertEqual(self.staging(), [])
        self.assertEqual(len(self.launcher.launched), 1)

    def test_other_actions_still_reject_trailing_bytes(self):
        message = {"version": 1, "request_id": "req-9", "action": "status", "application_id": "orders", "commit": None,
                   "arguments": {}}
        buffer = io.BytesIO()
        write_frame(buffer, message)
        code, response, _ = self.send(buffer.getvalue() + b"binary")
        self.assertEqual((code, response["error"]), (2, "invalid_request"))

    def test_a_docker_artifact_cannot_smuggle_a_payload(self):
        docker = {"kind": "docker", "image": "ghcr.io/example/orders@sha256:" + "b" * 64}
        code, response, _ = self.send(self.frame_with(b"smuggled", artifact=docker))
        self.assertEqual((code, response["error"]), (2, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
