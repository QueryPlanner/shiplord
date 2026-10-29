from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import shiplord.client as client_module  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.client import ClientError  # noqa: E402
from shiplord.commands import CommandError  # noqa: E402
from shiplord.manifest import validate_manifest  # noqa: E402
from shiplord.native import NativeRuntime  # noqa: E402
from shiplord.runtime import release_id  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from shiplord.worker import run_job  # noqa: E402
from test_client import ClientTestCase  # noqa: E402
from test_native import FakeSystem, elf  # noqa: E402
from test_native_helper import native_configuration  # noqa: E402

COMMIT = "a" * 40


def native_manifest():
    return validate_manifest(native_configuration())


class NativeCommandTests(ClientTestCase):
    RUNTIME = "native"

    def setUp(self):
        super().setUp()
        for target in ("shiplord.native.host_architecture", "shiplord.privileged_host.host_architecture"):
            patcher = patch(target, return_value="linux-amd64")  # not the machine running the tests
            patcher.start()
            self.addCleanup(patcher.stop)
        self.system = FakeSystem()
        self.system.results["show"] = CommandResult(0, "ActiveState=active\nSubState=running\n")
        native = NativeRuntime(self.system, self.layout)
        self.services.runtime_factory = lambda services, registration: native
        self.services.probes = type("P", (type(self.probes),), {
            "ready_once": lambda s, r, rel: True, "wait_ready": lambda s, r, rel, alive: {"attempts": 1, "ready": True}})(self.runtime)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.binary_path = Path(self.work.name) / "app"
        self.binary = elf(body=os.urandom(2048))
        self.binary_path.write_bytes(self.binary)
        original = client_module.wait_for_job
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        client_module.wait_for_job = lambda client, job_id, **options: (
            run_job(self.services, "orders", job_id), original(client, job_id, **dict(options, interval=0, sleep=lambda s: None)))[1]

    def deploy(self, **overrides):
        options = dict(binary=self.binary_path, commit=COMMIT, sequence=1000, request_id="req-1", timeout=60,
                       public_check=lambda domain, path: {"ok": True, "status": 200, "error": None})
        options.update(overrides)
        return commands.deploy_application(native_manifest(), self.client, **options)

    def test_the_local_artifact_description_is_exact_and_rejects_non_linux_binaries(self):
        artifact = commands.native_artifact(self.binary_path)
        self.assertEqual(artifact, {"kind": "native", "sha256": hashlib.sha256(self.binary).hexdigest(),
                                    "size": len(self.binary), "architecture": "linux-amd64"})
        macho = Path(self.work.name) / "mac"
        macho.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 100)
        with self.assertRaisesRegex(CommandError, "not a Linux ELF"):
            commands.native_artifact(macho)
        arm = Path(self.work.name) / "arm"
        arm.write_bytes(elf(machine=0xB7))
        self.assertEqual(commands.native_artifact(arm)["architecture"], "linux-arm64")  # described, not judged here
        odd = Path(self.work.name) / "odd"
        odd.write_bytes(elf(machine=0x28))
        with self.assertRaisesRegex(CommandError, "does not support"):
            commands.native_artifact(odd)
        for bad in (Path(self.work.name) / "missing",):
            with self.assertRaises(CommandError):
                commands.native_artifact(bad)
        empty = Path(self.work.name) / "empty"
        empty.write_bytes(b"")
        with self.assertRaises(CommandError):
            commands.native_artifact(empty)

    def test_a_native_deploy_uploads_installs_and_commits_and_reports_the_release(self):
        code, result = self.deploy()
        self.assertEqual((code, result["status"], result["release"]["commit"]), (0, "succeeded", COMMIT))
        committed = self.state.read_state()["current"]
        installed = self.layout.releases / "orders" / release_id(committed) / "app"
        self.assertEqual(installed.read_bytes(), self.binary)
        self.assertTrue(result["public"]["ok"])

    def test_flags_and_runtime_must_agree(self):
        image = "ghcr.io/example/orders@sha256:" + "b" * 64
        for kwargs in ({"binary": None}, {"image": image}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CommandError, "exactly one"):
                self.deploy(**kwargs)
        with self.assertRaisesRegex(CommandError, "native binary"):
            self.deploy(binary=None, image=image)
        docker = dict(native_manifest(), runtime="docker")
        with self.assertRaisesRegex(CommandError, "Docker image"):
            commands.deploy_application(docker, self.client, binary=self.binary_path, commit=COMMIT, sequence=1)

    def test_plan_describes_a_native_deploy_without_uploading_anything(self):
        code, plan = commands.plan_application(native_manifest(), self.client, binary=self.binary_path, commit=COMMIT,
                                               sequence=1000)
        self.assertEqual((code, plan["action"], plan["route_change"]), (0, "deploy", "create"))
        self.assertEqual(os.listdir(self.layout.app_state / "orders") if (self.layout.app_state / "orders").exists() else [],
                         ["state.json"])  # nothing staged

    def test_a_binary_for_the_other_architecture_is_refused_by_the_server_before_upload(self):
        arm = Path(self.work.name) / "arm"
        arm.write_bytes(elf(machine=0xB7, body=os.urandom(2048)))
        with self.assertRaises(ClientError) as caught:
            self.deploy(binary=arm)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("runs linux-amd64 binaries, not linux-arm64", str(caught.exception))
        self.assertEqual([p for p in (self.layout.app_state / "orders").iterdir() if p.is_dir()], [])  # nothing staged

    def test_a_corrupted_upload_is_refused_by_the_server_and_reported_as_invalid_input(self):
        original = self.client.payload_runner

        def corrupting(command, header, path, timeout):
            damaged = Path(self.work.name) / "damaged"
            damaged.write_bytes(path.read_bytes()[:-1] + b"X")
            return original(command, header, damaged, timeout)
        self.client.payload_runner = corrupting
        with self.assertRaises(ClientError) as caught:
            self.deploy()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("checksum", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
