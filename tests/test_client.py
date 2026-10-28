from __future__ import annotations

import io
import os
import sys
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import shiplord  # noqa: E402
from fakes import request  # noqa: E402
from shiplord.client import (AdminClient, ClientError, HelperClient, bootstrap_server, package_helper,  # noqa: E402
                             submit_and_wait, wait_for_job)
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.installer import CLIENT_MODULES, helper_modules  # noqa: E402
from shiplord.privileged_host import serve  # noqa: E402
from shiplord.runtime import RuntimeFailure, release_id  # noqa: E402
from shiplord.worker import run_job  # noqa: E402
from test_helper_actions import HelperTestCase  # noqa: E402


def frame(message) -> bytes:
    buffer = io.BytesIO()
    write_frame(buffer, message)
    return buffer.getvalue()


class ClientTestCase(HelperTestCase):
    """The client's runner is wired straight into the real privileged helper."""

    def setUp(self):
        super().setUp()
        self.commands: list[list[str]] = []
        patcher = patch("shiplord.privileged_host.authorize_application", lambda application_id: self.registration)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = HelperClient("orders", "admin@server.example.com", runner=self.runner,
                                   payload_runner=self.payload_runner)

    def payload_runner(self, command, header, path, timeout):
        self.commands.append(command)
        return self.runner(command, header + path.read_bytes(), timeout)

    def runner(self, command, payload, timeout):
        self.commands.append(command)
        output = io.BytesIO()
        code = serve(io.BytesIO(payload), output, self.factory)
        return output.getvalue(), b"", code

    def deploy_arguments_and_commit(self, **overrides):
        commit, arguments = self.deploy_arguments(**overrides)
        return commit, arguments


class HelperClientTests(ClientTestCase):
    def test_calls_go_to_the_application_login_through_the_dispatcher_with_host_key_checking(self):
        self.client.call("status")
        command = self.commands[0]
        self.assertEqual(command[-2:], ["ship-orders@server.example.com", "/usr/local/libexec/shiplord-dispatch"])
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)

    def test_errors_map_to_exit_codes(self):
        self.runner_response = None

        def respond(message):
            return lambda command, payload, timeout: (frame(message), b"", 1)
        cases = [({"status": "error", "error": "access_denied"}, 3), ({"status": "incompatible", "request_id": "r1",
                                                                       "supported_versions": [2]}, 4),
                 ({"status": "error", "request_id": "r1", "error": "invalid_request"}, 2),
                 ({"status": "error", "request_id": "r1", "error": "failed", "message": "disk full"}, 1)]
        for message, code in cases:
            with self.subTest(message=message):
                client = HelperClient("orders", "a@h", runner=respond(message))
                with self.assertRaises(ClientError) as caught:
                    client.call("status", request_id="r1")
                self.assertEqual(caught.exception.code, code)
        mismatched = HelperClient("orders", "a@h", runner=respond({"status": "ok", "request_id": "other", "result": {}}))
        with self.assertRaises(ClientError):
            mismatched.call("status", request_id="r1")
        garbage = HelperClient("orders", "a@h", runner=lambda c, p, t: (b"not a frame", b"", 0))
        with self.assertRaises(ClientError) as caught:
            garbage.call("status")
        self.assertEqual(caught.exception.code, 3)


class DeployFlowTests(ClientTestCase):
    def deploy(self, request_id="req-1", progress=None, **overrides):
        commit, arguments = self.deploy_arguments(**overrides)
        return submit_and_wait(self.client, "deploy", request_id=request_id, commit=commit, arguments=arguments,
                               timeout=60, on_progress=(progress or (lambda phase: None)), interval=0,
                               sleep=lambda seconds: self.work(request_id))

    def work(self, request_id):
        record = self.services_job(request_id)
        if record and record["state"] == "queued":
            run_job(self.services, "orders", request_id)

    def services_job(self, request_id):
        return self.call("status", job_id=request_id)[0]["result"]["job"]

    def test_deploy_submits_polls_and_returns_the_recorded_outcome_with_progress(self):
        phases = []
        outcome = self.deploy(progress=phases.append)
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("succeeded", 0))
        self.assertEqual(phases[0], "queued")
        self.assertEqual(phases[-1], "finished")
        self.assertEqual(self.launcher.launched, [("orders", "req-1")])

    def test_failure_outcomes_keep_their_exit_codes(self):
        self.deploy()
        bad = {"commit": "c" * 40, "sequence": 1001, "artifact": request(image="d")["artifact"],
               "configuration": request()["configuration"]}
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        outcome = self.deploy("req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("rolled_back", 5))

    def test_a_resent_request_after_success_is_answered_from_the_record(self):
        self.deploy()
        launched = len(self.launcher.launched)
        again = self.deploy()
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(len(self.launcher.launched), launched)

    def test_rejections_are_returned_immediately_without_polling(self):
        outcome = self.deploy(artifact={"kind": "docker", "image": "ghcr.io/example/other@sha256:" + "b" * 64})
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("rejected", 2))

    def test_a_vanished_worker_is_reported_as_interrupted_with_the_recovery_hint(self):
        commit, arguments = self.deploy_arguments()
        self.launcher.active = False
        outcome = submit_and_wait(self.client, "deploy", request_id="req-1", commit=commit, arguments=arguments,
                                  timeout=60, interval=0, sleep=lambda s: None)
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("interrupted", 6))
        self.assertIn("recover", outcome["detail"]["message"])

    def test_waiting_gives_up_at_the_deadline_and_says_the_job_may_still_run(self):
        commit, arguments = self.deploy_arguments()
        ticks = iter(range(0, 1000, 10))
        with self.assertRaisesRegex(ClientError, "may still be running"):
            submit_and_wait(self.client, "deploy", request_id="req-1", commit=commit, arguments=arguments, timeout=25,
                            interval=0, sleep=lambda s: None, clock=lambda: next(ticks))

    def test_brief_ssh_failures_while_polling_are_tolerated_but_persistent_ones_are_not(self):
        commit, arguments = self.deploy_arguments()
        self.client.call("deploy", request_id="req-1", commit=commit, arguments=arguments)
        run_job(self.services, "orders", "req-1")
        real, state = self.client.runner, {"failures": 0}

        def flaky(command, payload, timeout):
            if state["failures"] < state["limit"]:
                state["failures"] += 1
                raise ClientError("the SSH exchange timed out", 3)
            return real(command, payload, timeout)
        self.client.runner = flaky
        state["limit"] = 2
        self.assertEqual(wait_for_job(self.client, "req-1", timeout=60, interval=0, sleep=lambda s: None)["status"],
                         "succeeded")
        state.update(failures=0, limit=99)
        with self.assertRaises(ClientError):
            wait_for_job(self.client, "req-1", timeout=60, interval=0, sleep=lambda s: None)
        self.assertEqual(state["failures"], 5)


class FakeSsh:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, command, payload, timeout):
        self.calls.append((command, payload))
        return self.responses.pop(0)


def ok(result=None):
    return frame({"status": "ok", "request_id": "x", "result": result or {}})


class AdminClientTests(unittest.TestCase):
    def test_passwordless_sudo_never_prompts_and_sends_one_frame(self):
        ssh = FakeSsh((ok({"host_port": 20000}), b"", 0))
        prompts = []
        client = AdminClient("appuser@server", prompt=prompts.append, runner=ssh)
        self.assertEqual(client.call("provision", "orders", {"a": 1}), {"host_port": 20000})
        command, payload = ssh.calls[0]
        self.assertEqual(command[-1], "sudo -n /usr/local/sbin/shiplord-admin")
        self.assertEqual(prompts, [])
        message = read_frame(io.BytesIO(payload))
        self.assertEqual((message["action"], message["application_id"], message["arguments"]), ("provision", "orders", {"a": 1}))

    def test_a_password_is_asked_only_after_sudo_says_it_is_required_and_goes_in_stdin_not_argv(self):
        ssh = FakeSsh((b"", b"sudo: a password is required\n", 1), (ok(), b"", 0))
        prompts = []
        AdminClient("appuser@server", prompt=lambda text: prompts.append(text) or "hunter2", runner=ssh).call(
            "keys", "orders", {"operation": "list"})
        self.assertEqual(len(prompts), 1)
        first_command, first_payload = ssh.calls[0]
        second_command, second_payload = ssh.calls[1]
        self.assertIn("sudo -S -p ''", second_command[-1])
        self.assertTrue(second_payload.startswith(b"hunter2\n"))
        self.assertNotIn(b"hunter2", first_payload)
        for command in (first_command, second_command):
            self.assertNotIn("hunter2", " ".join(command))
        self.assertEqual(read_frame(io.BytesIO(second_payload[len(b"hunter2\n"):]))["action"], "keys")

    def test_a_rejected_password_or_sudo_failure_is_reported_without_echoing_the_password(self):
        ssh = FakeSsh((b"", b"sudo: a password is required\n", 1), (b"", b"Sorry, try again.\n", 1))
        with self.assertRaises(ClientError) as caught:
            AdminClient("appuser@server", prompt=lambda text: "hunter2", runner=ssh).call("keys", "orders", {})
        self.assertEqual(caught.exception.code, 3)
        self.assertNotIn("hunter2", str(caught.exception))
        denied = FakeSsh((b"", b"appuser is not in the sudoers file.\n", 1))
        with self.assertRaisesRegex(ClientError, "not in the sudoers"):
            AdminClient("appuser@server", runner=denied).call("keys", "orders", {})

    def test_server_errors_map_to_exit_codes(self):
        for message, code in (({"status": "error", "request_id": "x", "error": "denied"}, 3),
                              ({"status": "error", "request_id": "x", "error": "invalid_request"}, 2),
                              ({"status": "error", "request_id": "x", "error": "failed", "message": "port exhausted"}, 1),
                              ({"status": "incompatible", "request_id": "x", "supported_versions": [9]}, 4)):
            with self.subTest(message=message), self.assertRaises(ClientError) as caught:
                AdminClient("a@h", runner=FakeSsh((frame(message), b"", 1))).call("provision", "orders", {})
            self.assertEqual(caught.exception.code, code)


class BootstrapTests(unittest.TestCase):
    SOURCE = Path(shiplord.__file__).parent

    def test_the_upload_contains_exactly_the_server_modules_with_fixed_metadata_and_is_reproducible(self):
        data = package_helper(self.SOURCE)
        self.assertEqual(data, package_helper(self.SOURCE))
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            names = [member.name for member in archive.getmembers()]
            self.assertEqual(names, [f"shiplord/{name}" for name in helper_modules(self.SOURCE)])
            for member in archive.getmembers():
                self.assertEqual((member.uid, member.gid, member.mode, member.mtime), (0, 0, 0o644, 0))
        for client_only in CLIENT_MODULES:
            self.assertNotIn(f"shiplord/{client_only}", names)

    def test_bootstrap_uploads_runs_the_installer_under_sudo_and_always_cleans_up(self):
        result = {"version": "0.1.0-abc", "changed": {"code": True}}
        ssh = FakeSsh((b"/tmp/shiplord-install.Ab12Cd34\n", b"", 0), (frame({"status": "ok", "result": result}), b"", 0),
                      (b"", b"", 0))
        self.assertEqual(bootstrap_server("appuser@server", self.SOURCE, runner=ssh), result)
        upload, install, cleanup = ssh.calls
        self.assertIn("mktemp -d /tmp/shiplord-install.XXXXXXXX", upload[0][-1])
        self.assertTrue(tarfile.is_tarfile(io.BytesIO(upload[1])))
        self.assertTrue(install[0][-1].startswith("sudo -n /usr/bin/python3 -I -S -B -c "))
        self.assertTrue(install[0][-1].endswith("/tmp/shiplord-install.Ab12Cd34"))
        self.assertEqual(cleanup[0][-1], "rm -rf -- /tmp/shiplord-install.Ab12Cd34")

    def test_a_failed_install_still_removes_the_upload_and_reports_the_step(self):
        ssh = FakeSsh((b"/tmp/shiplord-install.Ab12Cd34\n", b"", 0),
                      (frame({"status": "error", "message": "Caddy is not installed"}), b"", 1), (b"", b"", 0))
        with self.assertRaisesRegex(ClientError, "Caddy is not installed"):
            bootstrap_server("appuser@server", self.SOURCE, runner=ssh)
        self.assertTrue(ssh.calls[-1][0][-1].startswith("rm -rf -- "))

    def test_an_unexpected_upload_directory_is_never_used(self):
        for output in (b"/etc\n", b"/tmp/shiplord-install.$(id)\n", b"", b"/tmp/shiplord-install.Ab12Cd34/../x\n"):
            with self.subTest(output=output), self.assertRaises(ClientError):
                bootstrap_server("appuser@server", self.SOURCE, runner=FakeSsh((output, b"", 0)))


if __name__ == "__main__":
    unittest.main()
