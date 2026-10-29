"""Graceful container replacement: policy validation, the stop procedure, and its transaction behavior."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import record, request  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.commands import CommandError  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.manifest import ManifestError, validate_manifest  # noqa: E402
from shiplord.release_state import StateError, validate_release  # noqa: E402
from shiplord.retire import make_retirer  # noqa: E402
from shiplord.runtime import DockerRuntime, RuntimeFailure, stop_signal  # noqa: E402
from shiplord.shutdown import check_budget, policy, ShutdownError  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from test_client import ClientTestCase  # noqa: E402
from test_commands import COMMIT, IMAGE  # noqa: E402
from test_deploy import DeployTestCase  # noqa: E402
from test_runtime import LEGACY_VOLUME, REGISTRATION, release  # noqa: E402

ID_A, ID_B = "a" * 64, "b" * 64


def configuration(**deploy):
    from fakes import configuration as base
    value = base()
    value["deploy"].update(deploy)
    return value


class ManifestTests(unittest.TestCase):
    def test_legacy_manifests_have_no_policy_fields_and_the_old_default_applies(self):
        result = validate_manifest(configuration())
        self.assertEqual(result["deploy"], {"readiness_timeout_seconds": 90, "retain_successful_releases": 2})
        self.assertEqual(policy(result), (10, "force"))

    def test_an_explicit_policy_round_trips(self):
        result = validate_manifest(configuration(shutdown_timeout_seconds=120, shutdown_timeout_action="fail"))
        self.assertEqual(policy(result), (120, "fail"))
        self.assertEqual(policy(validate_manifest(configuration(shutdown_timeout_seconds=1))), (1, "force"))

    def test_invalid_values_are_refused(self):
        for key, value in [("shutdown_timeout_seconds", v) for v in (0, 301, -1, True, "120", 1.5, None)] + \
                          [("shutdown_timeout_action", v) for v in ("kill", "FAIL", "", True, None, 1)]:
            with self.subTest(key=key, value=value), self.assertRaises(ManifestError):
                validate_manifest(configuration(**{key: value}))

    def test_the_bounds_are_inclusive(self):
        for seconds in (1, 300):
            validate_manifest(configuration(shutdown_timeout_seconds=seconds, readiness_timeout_seconds=30))

    def test_native_applications_refuse_the_docker_only_fields(self):
        for key, value in (("shutdown_timeout_seconds", 5), ("shutdown_timeout_action", "fail")):
            value = configuration(**{key: value})
            value["runtime"] = "native"
            with self.assertRaisesRegex(ManifestError, "only to Docker"):
                validate_manifest(value)

    def test_legacy_release_records_load_equal_to_their_raw_form(self):
        raw = record()
        self.assertEqual(validate_release(raw, "orders"), record())
        raw["configuration"]["deploy"]["shutdown_timeout_seconds"] = 500
        with self.assertRaises(StateError):
            validate_release(raw, "orders")


class BudgetTests(unittest.TestCase):
    def test_the_documented_120_second_drain_fits_the_job_limit(self):
        check_budget(validate_manifest(configuration(shutdown_timeout_seconds=120, shutdown_timeout_action="fail")))

    def test_a_policy_that_cannot_finish_inside_the_job_limit_is_refused(self):
        with self.assertRaisesRegex(ShutdownError, "exceeds the 3600s job limit"):
            check_budget(validate_manifest(configuration(shutdown_timeout_seconds=300, readiness_timeout_seconds=900)))


class SignalTests(unittest.TestCase):
    def test_default_and_catchable_signals_are_accepted(self):
        for configured, expected in (("", "SIGTERM"), ("<no value>", "SIGTERM"), ("SIGINT", "SIGINT"), ("quit", "SIGQUIT"),
                                     ("SIGUSR1", "SIGUSR1"), ("15", "15"), ("2", "2")):
            self.assertEqual(stop_signal(configured), expected)

    def test_uncatchable_or_unknown_signals_are_refused_before_anything_is_sent(self):
        for configured in ("SIGKILL", "KILL", "9", "SIGSTOP", "19", "0", "32", "SIGFOO", "TERM;rm", "-15"):
            with self.subTest(configured), self.assertRaises(RuntimeFailure):
                stop_signal(configured)


class ScriptedDocker:
    """A tiny stateful Docker: containers drain for ``drain`` seconds after SIGTERM, or never (``stubborn``)."""

    def __init__(self, clock):
        self.clock = clock
        self.containers: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.fail: dict[str, CommandResult] = {}
        self.volume = LEGACY_VOLUME

    def add(self, identifier, *, status="running", restart="unless-stopped", signal="", timeout="10", action="force",
            drain=0.0, stubborn=False, labels=True):
        self.containers[identifier] = {"status": status, "restart": restart, "signal": signal, "timeout": timeout,
                                       "action": action, "drain": drain, "stubborn": stubborn, "labels": labels,
                                       "exit_at": None}

    def _tick(self):
        for container in self.containers.values():
            if container["exit_at"] is not None and self.clock.now >= container["exit_at"]:
                container["status"], container["exit_at"] = "exited", None

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        argv = list(argv)
        self.calls.append(argv)
        self._tick()
        verb = argv[1]
        key = " ".join(argv[1:3]) if verb in ("volume", "image") else verb
        if key in self.fail:
            return self.fail[key]
        if key == "volume inspect":
            return self.volume
        if verb == "ps":
            return CommandResult(0, "\n".join(self.containers) + "\n")
        if verb == "inspect":
            container = self.containers.get(argv[-1])
            if container is None:
                return CommandResult(1, "Error: No such object")
            if "State.Status}}" in argv[3] and "RestartPolicy" not in argv[3]:
                return CommandResult(0, container["status"] + "\n")
            labels = (f"|{container['timeout']}|{container['action']}" if container["labels"] else "|<no value>|<no value>")
            return CommandResult(0, f"{container['status']} {container['restart']} {container['signal']}{labels}\n")
        if verb == "run":
            return CommandResult(0, "")
        container = self.containers.get(argv[-1])
        if container is None:
            return CommandResult(1, "Error: No such container")
        if verb == "update":
            container["restart"] = "no"
            return CommandResult(0, "")
        if verb == "kill":
            signal = argv[3]
            if signal == "KILL":
                container["status"] = "exited"
            elif not container["stubborn"]:
                container["exit_at"] = self.clock.now + container["drain"]
            return CommandResult(0, "")
        if verb == "rm":
            if "--force" in argv or "-f" in argv:
                raise AssertionError("graceful replacement must never force-remove")
            if container["status"] == "running":
                return CommandResult(1, "cannot remove a running container")
            del self.containers[argv[-1]]
            return CommandResult(0, "")
        return CommandResult(0, "")

    def sub(self, name):
        return [c for c in self.calls if c[1] == name]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class StopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        (Path(self.temp.name) / "environments" / "orders").mkdir(parents=True)
        self.clock = Clock()
        self.docker = ScriptedDocker(self.clock)
        self.runtime = DockerRuntime(self.docker, Layout(Path(self.temp.name).resolve()), platform_name="linux/amd64",
                                     clock=self.clock, sleep=self.clock.sleep)

    def replace(self):
        self.runtime.start(REGISTRATION, release())

    def test_a_process_that_exits_in_time_is_removed_without_force_before_the_candidate_starts(self):
        self.docker.add(ID_A, drain=3, timeout="10")
        self.replace()
        names = [c[1] for c in self.docker.calls]
        self.assertLess(names.index("kill"), names.index("rm"))
        self.assertLess(names.index("rm"), names.index("run"))
        self.assertEqual(self.docker.sub("kill")[0][2:], ["--signal", "SIGTERM", ID_A])
        self.assertEqual(len(self.docker.sub("kill")), 1)  # one signal, never a SIGKILL
        report = self.runtime.stop_report[0]
        self.assertEqual((report["forced"], report["action"], report["timeout_seconds"]), (False, "force", 10))
        self.assertIn("exited", report["stages"])

    def test_restart_is_disabled_before_the_signal_is_sent(self):
        self.docker.add(ID_A, drain=1)
        self.replace()
        names = [c[1] for c in self.docker.calls]
        self.assertLess(names.index("update"), names.index("kill"))
        self.assertEqual(self.docker.sub("update")[0][2:], ["--restart=no", ID_A])

    def test_force_kills_explicitly_after_the_deadline_and_reports_it(self):
        self.docker.add(ID_A, stubborn=True, timeout="5", action="force")
        self.replace()
        kills = [c[3] for c in self.docker.sub("kill")]
        self.assertEqual(kills, ["SIGTERM", "KILL"])
        self.assertGreaterEqual(self.clock.now, 5)
        report = self.runtime.stop_report[0]
        self.assertTrue(report["forced"])
        self.assertIn("forced", report["stages"])
        self.assertEqual(len(self.docker.sub("run")), 1)

    def test_fail_never_kills_removes_or_starts_a_candidate(self):
        self.docker.add(ID_A, stubborn=True, timeout="5", action="fail")
        with self.assertRaises(RuntimeFailure) as caught:
            self.replace()
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(caught.exception.kind, "shutdown_timeout")
        self.assertEqual([c[3] for c in self.docker.sub("kill")], ["SIGTERM"])
        self.assertEqual((self.docker.sub("rm"), self.docker.sub("run")), ([], []))
        self.assertEqual(self.docker.containers[ID_A]["status"], "running")
        self.assertIn("deadline_expired", self.runtime.stop_report[0]["stages"])

    def test_a_late_exit_just_before_the_deadline_is_a_clean_stop(self):
        self.docker.add(ID_A, drain=4.5, timeout="5", action="fail")
        self.replace()
        self.assertFalse(self.runtime.stop_report[0]["forced"])
        self.assertEqual(len(self.docker.sub("run")), 1)

    def test_a_continuing_attempt_is_not_signalled_again_and_fail_still_never_kills(self):
        self.docker.add(ID_A, restart="no", stubborn=True, action="fail")  # a previous worker already signalled it
        with self.assertRaises(RuntimeFailure) as caught:
            self.replace()
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual((self.docker.sub("kill"), self.docker.sub("update"), self.docker.sub("rm")), ([], [], []))
        self.assertTrue(self.runtime.stop_in_progress(REGISTRATION))

    def test_a_continuing_attempt_that_has_since_exited_is_removed_and_replaced(self):
        self.docker.add(ID_A, status="exited", restart="no", action="fail")
        self.replace()
        self.assertEqual(self.docker.sub("kill"), [])
        self.assertEqual(len(self.docker.sub("run")), 1)

    def test_a_continuing_force_attempt_waits_without_resignalling_and_then_kills(self):
        self.docker.add(ID_A, restart="no", stubborn=True, timeout="3", action="force")
        self.replace()
        self.assertEqual([c[3] for c in self.docker.sub("kill")], ["KILL"])

    def test_containers_that_are_already_stopped_are_removed_without_a_signal(self):
        self.docker.add(ID_A, status="exited")
        self.replace()
        self.assertEqual((self.docker.sub("kill"), self.docker.sub("update")), ([], []))
        self.assertEqual(self.docker.sub("rm"), [["/usr/bin/docker", "rm", ID_A]])

    def test_two_live_containers_are_ambiguous_and_nothing_is_signalled(self):
        self.docker.add(ID_A)
        self.docker.add(ID_B)
        with self.assertRaises(RuntimeFailure) as caught:
            self.replace()
        self.assertEqual(caught.exception.kind, "ambiguous")
        self.assertEqual((self.docker.sub("kill"), self.docker.sub("update"), self.docker.sub("rm")), ([], [], []))

    def test_malformed_policy_metadata_is_refused_before_any_signal(self):
        for timeout, action in (("0", "force"), ("301", "force"), ("abc", "force"), ("10", "kill"), ("10", "<no value>"),
                                ("<no value>", "force")):
            with self.subTest(timeout=timeout, action=action):
                self.docker.containers.clear()
                self.docker.calls.clear()
                self.docker.add(ID_A, timeout=timeout, action=action)
                with self.assertRaisesRegex(RuntimeFailure, "unusable shutdown metadata"):
                    self.replace()
                self.assertEqual(self.docker.sub("kill") + self.docker.sub("update") + self.docker.sub("rm"), [])

    def test_a_legacy_container_gets_the_old_ten_second_force_policy(self):
        self.docker.add(ID_A, labels=False, stubborn=True)
        self.replace()
        self.assertGreaterEqual(self.clock.now, 10)
        self.assertEqual([c[3] for c in self.docker.sub("kill")], ["SIGTERM", "KILL"])

    def test_a_custom_catchable_stop_signal_is_sent_and_an_uncatchable_one_is_refused(self):
        self.docker.add(ID_A, signal="SIGINT", drain=1)
        self.replace()
        self.assertEqual(self.docker.sub("kill")[0][3], "SIGINT")
        self.docker.containers.clear()
        self.docker.calls.clear()
        self.docker.add(ID_A, signal="SIGKILL")
        with self.assertRaises(RuntimeFailure):
            self.replace()
        self.assertEqual(self.docker.sub("kill") + self.docker.sub("update"), [])

    def test_a_paused_container_cannot_drain_and_needs_intervention(self):
        self.docker.add(ID_A, status="paused")
        with self.assertRaises(RuntimeFailure) as caught:
            self.replace()
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(self.docker.sub("kill"), [])

    def test_daemon_errors_never_authorise_force_even_with_the_force_policy(self):
        for failing in ("update", "kill", "inspect"):
            with self.subTest(failing):
                self.docker.containers.clear()
                self.docker.calls.clear()
                self.docker.fail = {failing: CommandResult(1, "Cannot connect to the Docker daemon")}
                self.docker.add(ID_A, stubborn=True, action="force")
                with self.assertRaises(RuntimeFailure) as caught:
                    self.replace()
                self.assertFalse(caught.exception.intervention_required)
                self.assertNotIn("KILL", [c[3] for c in self.docker.sub("kill") if len(c) > 3])
                self.assertEqual(self.docker.sub("run"), [])

    def test_a_container_that_disappears_during_the_wait_is_a_clean_stop(self):
        self.docker.add(ID_A, stubborn=True, timeout="5", action="fail")
        original = self.docker.run

        def vanish(argv, **kwargs):
            if argv[1] == "kill":
                self.docker.containers.pop(ID_A)
                return CommandResult(1, "Error: No such container")
            return original(argv, **kwargs)
        self.docker.run = vanish
        self.replace()
        self.assertEqual(len(self.docker.sub("run")), 1)

    def test_a_killed_container_that_never_exits_needs_intervention(self):
        self.docker.add(ID_A, stubborn=True, timeout="2", action="force")
        original = self.docker.run

        def survive(argv, **kwargs):
            if argv[1] == "kill" and argv[3] == "KILL":
                return CommandResult(0, "")
            return original(argv, **kwargs)
        self.docker.run = survive
        with self.assertRaises(RuntimeFailure) as caught:
            self.replace()
        self.assertEqual(caught.exception.kind, "kill_unconfirmed")

    def test_new_containers_record_their_policy_for_the_next_replacement(self):
        config = validate_manifest(configuration(shutdown_timeout_seconds=120, shutdown_timeout_action="fail"))
        value = release()
        value["configuration"] = config
        self.runtime.start(REGISTRATION, value)
        argv = self.docker.sub("run")[0]
        self.assertEqual(argv[argv.index("--stop-timeout") + 1], "120")
        self.assertIn("shiplord.shutdown_timeout=120", argv)
        self.assertIn("shiplord.shutdown_action=fail", argv)

    def test_a_legacy_release_records_ten_and_force(self):
        self.runtime.start(REGISTRATION, release())
        argv = self.docker.sub("run")[0]
        self.assertEqual(argv[argv.index("--stop-timeout") + 1], "10")
        self.assertIn("shiplord.shutdown_action=force", argv)

    def test_stopping_an_application_uses_the_same_graceful_rules(self):
        self.docker.add(ID_A, drain=2)
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.docker.sub("kill")[0][3], "SIGTERM")
        self.assertEqual(self.docker.containers, {})


class DeployerTests(DeployTestCase):
    def test_a_stop_deadline_with_fail_retains_the_journal_and_does_not_restore(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.runtime.calls.clear()
        self.runtime.start_errors[self.release_id("b")] = RuntimeFailure(
            "did not stop", "stop", intervention_required=True, kind="shutdown_timeout")
        outcome = self.deploy(request_id="req-2", image="b", commit="b", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))
        self.assertEqual(outcome.detail["kind"], "shutdown_timeout")
        self.assertIn("shiplord recover", outcome.detail["recovery"])
        self.assertIsNotNone(self.state.read_journal())
        self.assertEqual(self.runtime.names().count("start"), 1)  # no restoration attempt
        self.assertEqual(self.deploy(request_id="req-3", image="c", commit="c", sequence=1002).detail["error"],
                         "recovery_required")

    def release_id(self, image_char):
        from shiplord.runtime import release_id
        return release_id(record(image=image_char, commit=image_char, sequence=1001))

    def test_recovery_refuses_to_clear_the_journal_while_a_process_is_still_draining(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.runtime.start_errors[self.release_id("b")] = RuntimeFailure("x", "stop", intervention_required=True)
        self.deploy(request_id="req-2", image="b", commit="b", sequence=1001)
        self.runtime.stop_in_progress = lambda registration: True
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))
        self.assertIsNotNone(self.state.read_journal())
        self.runtime.stop_in_progress = lambda registration: False
        self.runtime.start_errors.clear()
        self.assertEqual(self.run_locked(self.deployer.recover).status, "recovered")
        self.assertIsNone(self.state.read_journal())

    def test_success_reports_the_shutdown(self):
        self.runtime.stop_report = [{"forced": True, "stages": ["forced"]}]
        self.assertEqual(self.deploy().detail["shutdown"], [{"forced": True, "stages": ["forced"]}])

    def test_plan_shows_the_outgoing_policy_and_the_candidates(self):
        self.assertEqual(self.deploy().status, "succeeded")
        value = request(request_id="req-2", image="b", commit="b", sequence=1001)
        value["configuration"] = configuration(shutdown_timeout_seconds=120, shutdown_timeout_action="fail")
        plan = self.run_locked(self.deployer.plan, value)
        self.assertEqual(plan["shutdown"]["outgoing_policy"], {"timeout_seconds": 10, "action": "force"})
        self.assertEqual(plan["shutdown"]["candidate_policy"], {"timeout_seconds": 120, "action": "fail"})

    def test_a_budget_that_cannot_fit_is_rejected_before_anything_changes(self):
        value = request()
        value["configuration"] = configuration(shutdown_timeout_seconds=300, readiness_timeout_seconds=900)
        outcome = self.run_locked(self.deployer.deploy, value)
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertEqual(self.runtime.calls, [])


class RetireTests(unittest.TestCase):
    def test_a_stop_timeout_during_retirement_reports_that_the_route_is_gone_and_data_is_kept(self):
        from shiplord.provision import ProvisionError

        class Runtime:
            def stop(self, registration):
                raise RuntimeFailure("did not stop", "stop", intervention_required=True)

        class Routes:
            removed = []

        with tempfile.TemporaryDirectory() as directory, \
                patch("shiplord.retire.RouteManager") as routes:
            retire = make_retirer(Layout(Path(directory)), None, None, None, runtime_for=lambda registration: Runtime())
            with self.assertRaisesRegex(ProvisionError, "route was already removed.*data are kept"):
                retire(REGISTRATION, {"current": None, "previous": None})
            routes.return_value.remove.assert_called_once_with("orders")


class NegotiationTests(ClientTestCase):
    def manifest(self):
        return validate_manifest(configuration(shutdown_timeout_seconds=120, shutdown_timeout_action="fail"))

    def test_a_helper_without_the_feature_is_refused_before_any_job(self):
        old = {"status": "ok", "request_id": "x", "version": 1, "helper_version": "0.1.0",
               "actions": ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover", "docker_storage_v1"]}
        with patch.object(self.client, "call", return_value=old) as call:
            with self.assertRaisesRegex(CommandError, "does not support shutdown_timeout"):
                commands.deploy_application(self.manifest(), self.client, image=IMAGE, commit=COMMIT, sequence=1)
            self.assertEqual([c.args[0] for c in call.call_args_list], ["capabilities"])

    def test_legacy_manifests_never_probe_capabilities(self):
        with patch.object(self.client, "call", side_effect=AssertionError("unexpected call")):
            commands.require_feature_support(validate_manifest(configuration()), self.client)

    def test_the_client_waits_for_both_possible_stops(self):
        legacy = validate_manifest(configuration())
        self.assertEqual(commands.job_timeout(legacy), 90 + 900)  # unchanged for existing manifests
        explicit = self.manifest()
        self.assertEqual(commands.job_timeout(explicit), 90 + 900 + 2 * (120 + 10))

    def test_a_plan_through_the_real_helper_shows_both_policies(self):
        code, plan = commands.plan_application(self.manifest(), self.client, image=IMAGE, commit=COMMIT, sequence=1000)
        self.assertEqual((code, plan["shutdown"]["candidate_policy"]), (0, {"timeout_seconds": 120, "action": "fail"}))


if __name__ == "__main__":
    unittest.main()
