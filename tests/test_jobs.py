from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from shiplord.jobs import (KEEP_JOBS, DetachedLauncher, JobError, JobStore, SystemdLauncher, unit_name)
from shiplord.system import CommandError, CommandResult


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.now = 1000
        self.store = JobStore("orders", self.fd, clock=lambda: self.now)


class StoreTests(StoreTestCase):
    def test_create_is_private_and_idempotent_for_the_same_request(self):
        record, created = self.store.create("req-1", "deploy", {"commit": "a" * 40})
        self.assertTrue(created)
        self.assertEqual((record["state"], record["phase"], record["submitted_at"]), ("queued", "queued", 1000))
        self.assertEqual(stat.S_IMODE((self.directory / "req-1.json").stat().st_mode), 0o600)
        again, created = self.store.create("req-1", "deploy", {"commit": "a" * 40})
        self.assertFalse(created)
        self.assertEqual(again, record)

    def test_a_request_id_cannot_be_reused_for_a_different_request(self):
        self.store.create("req-1", "deploy", {"commit": "a" * 40})
        for action, request in (("deploy", {"commit": "b" * 40}), ("rollback", {"commit": "a" * 40})):
            with self.subTest(action=action), self.assertRaisesRegex(JobError, "different request"):
                self.store.create("req-1", action, request)

    def test_lifecycle_records_phases_and_the_outcome_and_refuses_changes_after_finishing(self):
        self.store.create("req-1", "deploy", {})
        self.now = 1001
        self.assertEqual(self.store.start("req-1")["started_at"], 1001)
        self.store.set_phase("req-1", "runtime-ready")
        self.assertEqual(self.store.get("req-1")["phase"], "runtime-ready")
        self.now = 1005
        done = self.store.finish("req-1", {"status": "succeeded", "exit_code": 0, "detail": {}})
        self.assertEqual((done["state"], done["finished_at"], done["outcome"]["status"]), ("finished", 1005, "succeeded"))
        for call in (lambda: self.store.set_phase("req-1", "x"), lambda: self.store.finish("req-1", {})):
            with self.assertRaisesRegex(JobError, "already finished"):
                call()
        with self.assertRaisesRegex(JobError, "does not exist"):
            self.store.start("missing")

    def test_invalid_ids_actions_and_corrupt_records_are_refused(self):
        for bad in ("", "../x", "a/b", "a b", "x" * 129, None, ".hidden"):
            with self.subTest(bad=bad), self.assertRaises(JobError):
                self.store.get(bad)
        with self.assertRaises(JobError):
            self.store.create("req-1", "shell", {})
        self.store.create("req-2", "deploy", {})
        path = self.directory / "req-2.json"
        original = json.loads(path.read_text())
        for damage in ({"application_id": "billing"}, {"state": "weird"}, {"extra": 1}, {"schema_version": 2}):
            path.write_text(json.dumps(dict(original, **damage)))
            path.chmod(0o600)
            with self.subTest(damage=damage), self.assertRaises(StateErrorLike):
                self.store.get("req-2")
        path.write_text(json.dumps(dict(original, state="finished", outcome=None)))
        path.chmod(0o600)
        with self.assertRaisesRegex(JobError, "no outcome"):
            self.store.get("req-2")

    def test_the_store_lists_unfinished_jobs(self):
        self.store.create("a", "deploy", {})
        self.store.create("b", "deploy", {})
        self.store.start("b")
        self.store.create("c", "deploy", {})
        self.store.start("c")
        self.store.finish("c", {"status": "succeeded", "exit_code": 0, "detail": {}})
        self.assertEqual(sorted(self.store.unfinished()), ["a", "b"])

    def test_old_finished_jobs_are_pruned_but_unfinished_ones_are_kept(self):
        self.store.create("running", "deploy", {})
        self.store.start("running")
        for number in range(KEEP_JOBS + 5):
            self.now += 1
            self.store.create(f"job-{number}", "deploy", {})
            self.store.start(f"job-{number}")
            self.store.finish(f"job-{number}", {"status": "succeeded", "exit_code": 0, "detail": {}})
        names = {p.stem for p in self.directory.glob("*.json")}
        self.assertLessEqual(len(names), KEEP_JOBS + 1)
        self.assertIn("running", names)
        self.assertNotIn("job-0", names)
        self.assertIn(f"job-{KEEP_JOBS + 4}", names)

    def test_untrusted_directory_is_refused(self):
        self.directory.chmod(0o777)
        self.addCleanup(self.directory.chmod, 0o700)
        with self.assertRaises(ValueError):
            JobStore("orders", self.fd)


StateErrorLike = (JobError, ValueError)


class FakeSystem:
    def __init__(self, result=CommandResult(0, "")):
        self.calls, self.result, self.error = [], result, None

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        self.calls.append(list(argv))
        if self.error:
            raise self.error
        return self.result


class LauncherTests(unittest.TestCase):
    def test_systemd_launch_uses_a_transient_unit_and_passes_ids_as_separate_arguments(self):
        system = FakeSystem()
        SystemdLauncher(system).launch("orders", "req-1")
        self.assertEqual(system.calls, [["/usr/bin/systemd-run", "--unit", "shiplord-job-orders:req-1", "--collect",
                                         "--quiet", "--property=RuntimeMaxSec=3600", "--description", "Shiplord job orders req-1", "--",
                                         "/usr/local/sbin/shiplord-worker", "orders", "req-1"]])

    def test_unit_names_are_bounded_and_distinct_per_application(self):
        self.assertLessEqual(len(unit_name("a" * 48, "r" * 128)), 100)
        self.assertNotEqual(unit_name("orders", "req-1"), unit_name("billing", "req-1"))

    def test_no_two_application_and_request_pairs_can_share_a_unit_name(self):
        # Both parts may contain "-", so a "-" separator was ambiguous: these two pairs used to collide.
        self.assertNotEqual(unit_name("orders", "staging-gh-123-1"), unit_name("orders-staging", "gh-123-1"))
        for app in ("orders", "job-x"):  # and no job unit can equal a native application's service unit
            self.assertFalse(unit_name(app, "r") + ".service" == f"shiplord-{app}.service")
        self.assertEqual(unit_name("job-x", "R").split(":")[0], "shiplord-job-job-x")

    def test_launch_failures_are_reported(self):
        failing = FakeSystem(CommandResult(1, "Failed to connect to bus"))
        with self.assertRaises(JobError):
            SystemdLauncher(failing).launch("orders", "req-1")
        broken = FakeSystem()
        broken.error = CommandError("x", "start")
        with self.assertRaises(JobError):
            SystemdLauncher(broken).launch("orders", "req-1")

    def test_is_active_reads_systemctl(self):
        for code, output, expected in ((0, "active\n", True), (0, "activating\n", True), (3, "inactive\n", False),
                                       (3, "failed\n", False), (0, "weird\n", False)):
            with self.subTest(output=output):
                system = FakeSystem(CommandResult(code, output))
                self.assertEqual(SystemdLauncher(system).is_active("orders", "req-1"), expected)
                self.assertEqual(system.calls[0][:2], ["/usr/bin/systemctl", "is-active"])
        broken = FakeSystem()
        broken.error = CommandError("x", "timeout")
        self.assertFalse(SystemdLauncher(broken).is_active("orders", "req-1"))


class DetachedLauncherTests(unittest.TestCase):
    def test_runs_the_worker_detached_with_the_ids_and_reports_activity(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker"
            script = Path(temp) / "worker.py"
            script.write_text(textwrap.dedent(f'''
                import os, sys, time
                open({str(marker)!r}, "w").write(" ".join(sys.argv[1:]) + " " + str(os.getsid(0) == os.getpid()))
                time.sleep(2)
            '''))
            launcher = DetachedLauncher([sys.executable, str(script)])
            launcher.launch("orders", "req-1")
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(marker.read_text(), "orders req-1 True")  # its own session: it outlives the caller
            self.assertTrue(launcher.is_active("orders", "req-1"))
            launcher.processes[unit_name("orders", "req-1")].wait(10)
            self.assertFalse(launcher.is_active("orders", "req-1"))
            self.assertFalse(launcher.is_active("orders", "other"))

    def test_a_missing_executable_is_a_job_error(self):
        with self.assertRaises(JobError):
            DetachedLauncher(["/nonexistent/worker"]).launch("orders", "req-1")


if __name__ == "__main__":
    unittest.main()
