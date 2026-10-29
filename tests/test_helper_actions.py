from __future__ import annotations

import io
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeCaddy, FakeProbes, FakeRuntime, configuration, record, request  # noqa: E402
from shiplord.host import HostDeadline  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.jobs import JobError  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.privileged_host import dispatch, serve  # noqa: E402
from shiplord.protocol import PROTOCOL_VERSION, ProtocolError  # noqa: E402
from shiplord.registry import Registry  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState  # noqa: E402
from shiplord.runtime import RuntimeFailure, release_id  # noqa: E402
from shiplord.services import HostServices  # noqa: E402
from shiplord.worker import run_job  # noqa: E402


class FakeLauncher:
    def __init__(self):
        self.launched: list[tuple[str, str]] = []
        self.active = True  # systemd-run returns once the unit is running
        self.error = None

    def launch(self, application_id, request_id):
        if self.error:
            raise self.error
        self.launched.append((application_id, request_id))

    def is_active(self, application_id, request_id):
        return self.active


class HelperTestCase(unittest.TestCase):
    RUNTIME = "docker"

    def setUp(self):
        # Serving an upload re-arms the process-wide deadline timer, which only run_timed() normally cancels.
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        patcher = patch("os.fsync", lambda descriptor: None)  # durability is covered by the journal tests
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        for directory, mode in ((self.layout.var, 0o755), (self.layout.registrations, 0o700), (self.layout.app_state, 0o700),
                                (self.layout.jobs, 0o700), (self.layout.caddy_root / "ship", 0o755)):
            directory.mkdir(parents=True)
            directory.chmod(mode)
        self.layout.caddy_root.chmod(0o755)
        main = self.layout.caddy_root / "Caddyfile"
        main.write_text(f"import {self.layout.caddy_root}/ship/*.caddy\n")
        main.chmod(0o644)
        var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        registry_fd = os.open(self.layout.registrations, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, var_fd)
        self.addCleanup(os.close, registry_fd)
        lock = HostLock(var_fd)
        with lock.acquire():
            self.registration = Registry(registry_fd, lock).register(
                "orders", deployment_uid=1001, deployment_user="ship-orders", runtime=self.RUNTIME,
                api_domain="orders.example.com",
                image_repository="ghcr.io/example/orders" if self.RUNTIME == "docker" else None, probe=lambda port: True)
            (self.layout.app_state / "orders").mkdir(mode=0o700)
            state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
            ReleaseState("orders", state_fd, lock).initialize()
            os.close(state_fd)
        self.launcher, self.caddy, self.runtime = FakeLauncher(), FakeCaddy(), FakeRuntime()
        self.probes = FakeProbes(self.runtime)
        self.services = HostServices(self.layout, system=None, launcher=self.launcher, caddy=self.caddy,
                                     probes=self.probes, runtime_factory=lambda services, registration: self.runtime, lock_timeout=0.3)
        self.factory_calls = 0

    def factory(self):
        self.factory_calls += 1
        return self.services

    def envelope(self, action, request_id="req-1", commit=None, **arguments):
        return {"version": PROTOCOL_VERSION, "request_id": request_id, "action": action, "application_id": "orders",
                "commit": commit, "arguments": arguments}

    def call(self, action, request_id="req-1", commit=None, **arguments):
        return dispatch(self.envelope(action, request_id, commit, **arguments), self.registration, self.factory)

    def deploy_arguments(self, **overrides):
        value = request(**overrides)
        return value["commit"], {"sequence": value["sequence"], "artifact": value["artifact"],
                                 "configuration": value["configuration"]}

    def submit_deploy(self, request_id="req-1", **overrides):
        commit, arguments = self.deploy_arguments(**overrides)
        return self.call("deploy", request_id, commit, **arguments)

    @property
    def state(self):
        descriptor = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        return ReleaseState("orders", descriptor, HostLock(descriptor))


class ReadOnlyActionTests(HelperTestCase):
    def test_capabilities_advertise_every_supported_action_without_touching_the_host(self):
        response, code = self.call("capabilities")
        self.assertEqual((code, response["status"]), (0, "ok"))
        self.assertEqual(response["actions"], ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover", "docker_storage_v1"])
        self.assertEqual(self.factory_calls, 0)

    def test_malformed_arguments_are_rejected_before_any_host_state_is_opened(self):
        bad = [("status", {"unexpected": 1}), ("status", {"job_id": "../x"}) if False else ("logs", {"tail": 0}),
               ("logs", {"tail": 1001}), ("logs", {"tail": "5"}), ("logs", {"tail": True}), ("deploy", {}),
               ("deploy", {"sequence": 1, "artifact": {}}), ("plan", {"bogus": 1}), ("rollback", {"x": 1}),
               ("recover", {"x": 1})]
        for action, arguments in bad:
            with self.subTest(action=action, arguments=arguments), self.assertRaises(ProtocolError):
                self.call(action, **arguments)
        with self.assertRaises(ProtocolError):
            self.call("rollback", commit="a" * 40)
        with self.assertRaises(ProtocolError):
            self.call("deploy", commit="a" * 40, sequence=1, artifact={}, configuration={}, allow_older="yes")
        self.assertEqual(self.factory_calls, 0)

    def test_status_reports_the_application_and_an_optional_job(self):
        response, code = self.call("status")
        self.assertEqual(code, 0)
        self.assertEqual(response["result"]["application"]["current"], None)
        self.assertIsNone(response["result"]["job"])
        self.assertIsNone(self.call("status", job_id="nope")[0]["result"]["job"])
        with self.assertRaises(Exception):
            self.call("status", job_id="../etc/passwd")

    def test_logs_return_the_runtime_output(self):
        self.runtime.logs = lambda registration, tail: f"tail={tail}\n"
        self.assertEqual(self.call("logs", tail=7)[0]["result"], {"logs": "tail=7\n"})
        self.assertEqual(self.call("logs")[0]["result"], {"logs": "tail=100\n"})

    def test_plan_describes_the_change_and_changes_nothing(self):
        commit, arguments = self.deploy_arguments()
        response, code = self.call("plan", "plan-1", commit, **arguments)
        plan = response["result"]
        self.assertEqual((code, plan["action"], plan["route_change"], plan["restarts_application"]),
                         (0, "deploy", "create", False))
        self.assertEqual((plan["domain"], plan["host_port"]), ("orders.example.com", self.registration["host_port"]))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(os.listdir(self.layout.caddy_root / "ship"), [])
        self.assertEqual(self.state.read_state()["generation"], 0)

    def test_plan_reports_refusals_instead_of_raising(self):
        commit, arguments = self.deploy_arguments()
        arguments["artifact"] = {"kind": "docker", "image": "ghcr.io/example/other@sha256:" + "b" * 64}
        plan = self.call("plan", "plan-1", commit, **arguments)[0]["result"]
        self.assertEqual((plan["action"], plan["reason"]), ("none", "rejected"))


class DeployJobTests(HelperTestCase):
    def test_deploy_accepts_a_job_launches_the_worker_once_and_is_idempotent(self):
        response, code = self.submit_deploy()
        self.assertEqual((code, response["status"], response["job"]["state"]), (0, "accepted", "queued"))
        self.assertEqual(self.launcher.launched, [("orders", "req-1")])
        self.assertEqual(self.runtime.calls, [])  # the helper itself deploys nothing
        again, code = self.submit_deploy()
        self.assertEqual((code, again["status"]), (0, "accepted"))
        self.assertEqual(self.launcher.launched, [("orders", "req-1")])  # not launched twice

    def test_retrying_a_failed_request_returns_its_recorded_outcome_not_a_reuse_error(self):
        self.runtime.prepare_error = RuntimeFailure("denied", "prepare")
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        response, code = self.submit_deploy()
        self.assertEqual((code, response["status"], response["job"]["outcome"]["detail"]["error"]),
                         (0, "accepted", "prepare_failed"))
        self.assertEqual(len(self.launcher.launched), 1)

    def test_the_same_request_id_with_different_content_is_refused(self):
        self.submit_deploy()
        with self.assertRaises(JobError):
            self.submit_deploy(commit="c")

    def test_invalid_requests_are_rejected_immediately_without_creating_a_job(self):
        for overrides in ({"artifact": {"kind": "docker", "image": "ghcr.io/example/other@sha256:" + "b" * 64}},
                          {"configuration": configuration(api_domain="evil.example.com")}):
            with self.subTest(overrides=str(overrides)[:50]):
                response, code = self.submit_deploy(**overrides)
                self.assertEqual((code, response["status"], response["outcome"]["status"]), (2, "finished", "rejected"))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.launcher.launched, [])

    def test_a_launch_failure_finishes_the_job_as_failed_and_is_reported(self):
        self.launcher.error = JobError("systemd unavailable")
        response, code = self.submit_deploy()
        self.assertEqual((code, response["error"], response["job"]["state"]), (1, "launch_failed", "finished"))
        self.assertEqual(response["job"]["outcome"]["exit_code"], 1)

    def test_a_job_whose_worker_vanished_is_reported_as_interrupted(self):
        self.submit_deploy()
        self.assertEqual(self.call("status", job_id="req-1")[0]["result"]["job"]["state"], "queued")
        self.launcher.active = False
        self.assertEqual(self.call("status", job_id="req-1")[0]["result"]["job"]["state"], "interrupted")

    def test_a_flood_of_distinct_requests_is_capped_but_dead_jobs_do_not_count(self):
        from shiplord.jobs import MAX_PENDING_JOBS
        for number in range(MAX_PENDING_JOBS):
            self.assertEqual(self.submit_deploy(f"req-{number}", sequence=1000 + number)[1], 0)
        response, code = self.submit_deploy("one-too-many", sequence=2000)
        self.assertEqual((code, response["error"]), (1, "too_many_pending"))
        self.assertEqual(len(self.launcher.launched), MAX_PENDING_JOBS)
        self.assertEqual(self.submit_deploy("req-0", sequence=1000)[1], 0)  # a repeat of a known request is not new work
        self.launcher.active = False  # their workers are gone: they are interrupted, not pending
        self.assertEqual(self.submit_deploy("after-the-crash", sequence=3000)[1], 0)

    def test_rollback_and_recover_become_jobs_too(self):
        for action in ("rollback", "recover"):
            response, code = self.call(action, f"{action}-1")
            self.assertEqual((code, response["job"]["action"]), (0, action))
        self.assertEqual([r for _, r in self.launcher.launched], ["rollback-1", "recover-1"])


class WorkerTests(HelperTestCase):
    def finished(self, request_id="req-1"):
        return self.call("status", job_id=request_id)[0]["result"]["job"]

    def test_the_worker_runs_the_deploy_and_records_the_outcome_for_a_reconnecting_client(self):
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        job = self.finished()
        self.assertEqual((job["state"], job["outcome"]["status"], job["outcome"]["exit_code"]), ("finished", "succeeded", 0))
        self.assertEqual(job["outcome"]["detail"]["release"]["commit"], "a" * 40)
        self.assertEqual(self.state.read_state()["current"], record())
        self.assertEqual(self.runtime.running, {release_id(record())})
        # a duplicate worker, or a status poll, never reruns or alters a finished job
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        self.assertEqual(self.runtime.names(), ["prepare", "start"])

    def test_a_deploy_resent_after_success_is_answered_without_a_new_job(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        launched = len(self.launcher.launched)
        response, code = self.submit_deploy()
        self.assertEqual(code, 0)
        self.assertEqual(response["status"], "accepted")  # the existing finished job is returned as is
        self.assertEqual(response["job"]["state"], "finished")
        self.assertEqual(len(self.launcher.launched), launched)

    def test_failure_exit_codes_are_recorded_and_returned(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        self.submit_deploy("req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(run_job(self.services, "orders", "req-2"), 5)
        job = self.finished("req-2")
        self.assertEqual((job["outcome"]["status"], job["outcome"]["exit_code"]), ("rolled_back", 5))

    def test_phases_are_visible_while_the_job_runs(self):
        seen = []
        original = self.runtime.start

        def spying_start(registration, release):
            seen.append(self.finished()["phase"])
            return original(registration, release)
        self.runtime.start = spying_start
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.assertEqual(seen, ["runtime-changing"])

    def test_a_busy_host_lock_is_reported_and_nothing_is_changed(self):
        self.submit_deploy()
        descriptor = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        with HostLock(descriptor).acquire():
            self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        job = self.finished()
        self.assertEqual(job["outcome"]["detail"]["error"], "host_busy")
        self.assertEqual(self.runtime.calls, [])

    def test_an_unexpected_error_before_any_change_is_a_plain_failure(self):
        self.runtime.prepare = lambda registration, release: (_ for _ in ()).throw(KeyError("surprise"))
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        self.assertEqual(self.finished()["outcome"]["detail"], {"error": "worker_error", "message": "KeyError"})

    def test_an_unexpected_error_mid_transaction_requires_intervention_and_keeps_the_journal(self):
        self.runtime.start = lambda registration, release: (_ for _ in ()).throw(RuntimeError("bug"))
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 6)
        self.assertEqual(self.finished()["outcome"]["status"], "intervention_required")
        self.assertIsNotNone(self.state.read_journal())

    def test_unknown_or_unqueued_jobs_do_nothing(self):
        self.assertEqual(run_job(self.services, "orders", "never-created"), 2)
        self.assertEqual(self.runtime.calls, [])

    def test_recover_and_rollback_jobs_run_through_the_same_engine(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.submit_deploy("req-2", commit="c", image="d", sequence=1001)
        run_job(self.services, "orders", "req-2")
        self.call("rollback", "rb-1")
        self.assertEqual(run_job(self.services, "orders", "rb-1"), 0)
        self.assertEqual(self.state.read_state()["current"]["commit"], "a" * 40)
        self.call("recover", "rc-1")
        self.assertEqual(run_job(self.services, "orders", "rc-1"), 0)
        self.assertEqual(self.finished("rc-1")["outcome"]["status"], "nothing_to_recover")


class ServeTests(HelperTestCase):
    def serve(self, message):
        encoded, output = io.BytesIO(), io.BytesIO()
        write_frame(encoded, message)
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration):
            code = serve(io.BytesIO(encoded.getvalue()), output, self.factory)
        return code, read_frame(io.BytesIO(output.getvalue()))

    def test_frames_round_trip_for_a_deploy_submission_and_a_status_poll(self):
        commit, arguments = self.deploy_arguments()
        code, response = self.serve(self.envelope("deploy", "req-1", commit, **arguments))
        self.assertEqual((code, response["status"], response["request_id"]), (0, "accepted", "req-1"))
        run_job(self.services, "orders", "req-1")
        code, response = self.serve(self.envelope("status", "poll-1", job_id="req-1"))
        self.assertEqual((code, response["result"]["job"]["outcome"]["status"]), (0, "succeeded"))

    def test_protocol_and_state_errors_map_to_exit_codes_without_leaking_internals(self):
        code, response = self.serve(self.envelope("logs", tail=0))
        self.assertEqual((code, response["error"]), (2, "invalid_request"))
        code, response = self.serve(dict(self.envelope("status"), version=2))
        self.assertEqual((code, response["status"]), (4, "incompatible"))
        self.services.layout = Layout(Path(self.temp.name) / "missing")
        code, response = self.serve(self.envelope("status"))
        self.assertEqual((code, response["error"]), (1, "failed"))
        self.assertNotIn(self.temp.name, str(response))  # host paths are not disclosed to the deployment account

    def test_the_deadline_is_not_swallowed_by_the_generic_failure_handler(self):
        def expired():
            raise HostDeadline()
        encoded = io.BytesIO()
        write_frame(encoded, self.envelope("status"))
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration):
            with self.assertRaises(HostDeadline):
                serve(io.BytesIO(encoded.getvalue()), io.BytesIO(), expired)

    def test_an_unforeseen_exception_is_a_generic_failure_without_a_traceback_or_paths(self):
        def broken():
            raise RuntimeError(f"boom at {self.temp.name}/secret")
        encoded, output = io.BytesIO(), io.BytesIO()
        write_frame(encoded, self.envelope("status"))
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration):
            code = serve(io.BytesIO(encoded.getvalue()), output, broken)
        response = read_frame(io.BytesIO(output.getvalue()))
        self.assertEqual((code, response["error"]), (1, "failed"))
        self.assertNotIn("boom", str(response))
        self.assertNotIn(self.temp.name, str(response))


if __name__ == "__main__":
    unittest.main()
