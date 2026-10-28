from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from shiplord.release_state import HostLock, ReleaseState, StateError


def release(commit="a", image="b"):
    return {"commit": commit * 40, "sequence": 1000, "artifact": {"kind": "docker", "image": "ghcr.io/example/orders@sha256:" + image * 64},
            "configuration": {"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk",
                              "runtime": "docker", "http": {"api_domain": "api.example.com", "frontend_origins": [],
                              "container_port": 8080, "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
                              "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}}


class ReleaseStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.lock = HostLock(self.fd)
        self.store = ReleaseState("orders", self.fd, self.lock)
        with self.lock.acquire():
            self.store.initialize()

    def test_release_requires_ordered_phases_and_preserves_previous_release(self):
        with self.lock.acquire():
            self.store.begin("request-1", release())
            with self.assertRaises(StateError):
                self.store.record_commit()
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                self.store.advance(phase)
            self.store.record_commit()
            self.assertEqual(self.store.reconciliation_target(), release())
            self.store.clear_after_reconciliation()
            self.store.begin("request-2", release("c", "d"))
            self.assertEqual(self.store.reconciliation_target(), release())
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                self.store.advance(phase)
            self.store.record_commit()
            self.assertEqual(self.store.read_state()["previous"], release())
            self.store.clear_after_reconciliation()
        self.assertIsNone(self.store.read_journal())

    def test_first_release_failure_targets_empty_state_and_blocks_new_transaction(self):
        with self.lock.acquire():
            self.store.begin("request-1", release())
            self.assertIsNone(self.store.reconciliation_target())
            with self.assertRaises(StateError):
                self.store.begin("request-2", release())
            with self.assertRaises(StateError):
                self.store.advance("routing-ready")
            self.store.clear_after_reconciliation()

    def test_mutations_require_lock_and_existing_state(self):
        for operation in (self.store.initialize, lambda: self.store.begin("request-1", release()),
                          lambda: self.store.advance("runtime-changing"), self.store.record_commit,
                          self.store.clear_after_reconciliation):
            with self.subTest(operation=operation), self.assertRaises(StateError):
                operation()
        with self.lock.acquire():
            with self.assertRaises(StateError):
                self.store.initialize()
            with self.assertRaises(StateError):
                self.store.advance("runtime-changing")
            with self.assertRaises(StateError):
                self.store.record_commit()
            with self.assertRaises(StateError):
                self.store.clear_after_reconciliation()

    def test_invalid_artifacts_and_configuration_are_rejected_before_journal(self):
        examples = [dict(release(), commit="bad"), dict(release(), artifact={"kind": "docker", "image": "latest"}),
                    dict(release(), artifact={"kind": "native", "sha256": "bad", "size": 1, "architecture": "linux-amd64"}),
                    dict(release(), artifact={"kind": "other"}), dict(release(), configuration={}),
                    dict(release(), extra="secretvalue")]
        mismatch = release()
        mismatch["configuration"]["runtime"] = "native"
        examples.append(mismatch)
        with self.lock.acquire():
            for item in examples:
                with self.subTest(item=item), self.assertRaises(StateError):
                    self.store.begin("request-1", item)
            self.assertIsNone(self.store.read_journal())
            with self.assertRaises(StateError):
                self.store.begin("--bad", release())

    def test_native_artifact_identity_is_recorded(self):
        candidate = release()
        candidate["artifact"] = {"kind": "native", "sha256": "a" * 64, "size": 10, "architecture": "linux-amd64"}
        candidate["configuration"]["runtime"] = "native"
        with self.lock.acquire():
            self.store.begin("native-1", candidate)
        self.assertEqual(self.store.read_journal()["candidate"]["artifact"], candidate["artifact"])

    def test_corrupt_or_unsafe_state_fails_closed(self):
        state = self.directory / "state.json"
        for content in ('{"schema_version":1,"schema_version":1}', '[]', '{"schema_version":2}', 'x' * 65537):
            with self.subTest(content=content[:30]):
                state.write_text(content)
                with self.assertRaises(StateError):
                    self.store.read_state()
        state.unlink()
        state.symlink_to(self.directory / "elsewhere")
        with self.assertRaises(StateError):
            self.store.read_state()

    def test_state_and_journal_disagreement_requires_manual_intervention(self):
        with self.lock.acquire():
            self.store.begin("request-1", release())
            state = self.directory / "state.json"
            state.write_text(json.dumps({"schema_version": 1, "generation": 1, "transaction_id": "other",
                                         "current": release("c", "d"), "previous": None, "seen_requests": ["other"]}))
            with self.assertRaisesRegex(StateError, "manual intervention"):
                self.store.reconciliation_target()
            with self.assertRaises(StateError):
                self.store.advance("runtime-changing")

    def test_interrupted_write_never_claims_a_commit(self):
        with self.lock.acquire():
            with patch("shiplord.release_state.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(StateError):
                    self.store.begin("request-1", release())
            self.assertEqual(self.store.read_state()["generation"], 0)
            self.assertIsNone(self.store.read_journal())

    def test_lock_cannot_be_used_by_a_different_thread(self):
        results = []
        with self.lock.acquire():
            self.store.begin("request-1", release())
            def mutate():
                try:
                    self.store.clear_after_reconciliation()
                except StateError:
                    results.append("denied")
            worker = threading.Thread(target=mutate)
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, ["denied"])
            self.assertIsNotNone(self.store.read_journal())

    def test_removal_sync_failure_cannot_be_silently_bypassed(self):
        with self.lock.acquire():
            self.store.begin("request-1", release())
            with patch("shiplord.release_state.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(StateError):
                    self.store.clear_after_reconciliation()
                with self.assertRaisesRegex(StateError, "could not be synced"):
                    self.store.begin("request-2", release())
            self.store.begin("request-2", release())

    def test_committed_state_with_precommit_phase_is_corruption(self):
        with self.lock.acquire():
            self.store.begin("request-1", release())
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                self.store.advance(phase)
            self.store.record_commit()
            journal_path = self.directory / "journal.json"
            for phase in ("prepared", "runtime-changing", "runtime-ready", "routing-changing"):
                journal = json.loads(journal_path.read_text())
                journal["phase"] = phase
                journal_path.write_text(json.dumps(journal))
                with self.subTest(phase=phase), self.assertRaisesRegex(StateError, "manual intervention"):
                    self.store.reconciliation_target()

    def test_reused_request_ids_remain_reserved_after_later_transactions(self):
        with self.lock.acquire():
            for request_id in ("first", "second"):
                self.store.begin(request_id, release())
                for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                    self.store.advance(phase)
                self.store.record_commit()
                self.store.clear_after_reconciliation()
            with self.assertRaisesRegex(StateError, "already used"):
                self.store.begin("first", release("c", "d"))

    def test_the_request_history_is_a_window_so_an_application_never_runs_out_of_transactions(self):
        from shiplord.release_state import MAX_REQUEST_IDS
        phases = ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready")
        with patch("os.fsync", lambda descriptor: None), self.lock.acquire():
            for number in range(MAX_REQUEST_IDS + 40):  # more committed transactions than the window holds
                self.store.begin(f"commit-{number}", release(commit=f"{number % 9 + 1}"))
                for phase in phases:
                    self.store.advance(phase)
                self.store.record_commit()
                self.store.clear_after_reconciliation()
            state = self.store.read_state()
            self.assertEqual(len(state["seen_requests"]), MAX_REQUEST_IDS)
            self.assertEqual(state["transaction_id"], f"commit-{MAX_REQUEST_IDS + 39}")
            self.assertIn("commit-40", state["seen_requests"])
            self.assertNotIn("commit-0", state["seen_requests"])  # the oldest fell out
            for number in range(MAX_REQUEST_IDS + 40):  # then more failed transactions than the window holds
                self.store.begin(f"failed-{number}", release())
                self.store.reconciliation_target()
                self.store.clear_after_reconciliation()
            state = self.store.read_state()
            self.assertEqual(len(state["seen_requests"]), MAX_REQUEST_IDS)
            self.assertIn(state["transaction_id"], state["seen_requests"])  # the committed ID is never dropped
            self.assertEqual(state["transaction_id"], f"commit-{MAX_REQUEST_IDS + 39}")
            self.store.begin("one-more", release("e", "f"))  # still accepts work
            self.assertIn("failed-%d" % (MAX_REQUEST_IDS + 39), self.store.read_state()["seen_requests"])

    def test_failed_sync_after_state_publication_keeps_the_journal(self):
        with self.lock.acquire():
            self.store.begin("request-1", release())
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                self.store.advance(phase)
            original_sync = os.fsync
            calls = 0
            def failing_sync(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory sync failure")
                return original_sync(fd)
            with patch("shiplord.release_state.os.fsync", side_effect=failing_sync):
                with self.assertRaisesRegex(StateError, "may have been published"):
                    self.store.record_commit()
            self.assertEqual(self.store.reconciliation_target(), release())
            self.assertIsNotNone(self.store.read_journal())

    def test_process_kill_releases_lock_and_preserves_journal(self):
        script = '''import json, os, sys, time
from shiplord.release_state import HostLock, ReleaseState
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
lock = HostLock(fd)
store = ReleaseState("orders", fd, lock)
with lock.acquire():
    store.begin("child-" + sys.argv[3], json.loads(sys.argv[2]))
    if sys.argv[3] == "commit":
        for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
            store.advance(phase)
        store.record_commit()
    print("ready", flush=True)
    time.sleep(60)
'''
        for stage in ("before", "commit"):
            with self.subTest(stage=stage):
                process = subprocess.Popen([sys.executable, "-c", script, str(self.directory), json.dumps(release()), stage],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                    with self.assertRaisesRegex(StateError, "busy"):
                        with HostLock(self.fd).acquire(timeout=0.1):
                            pass
                    self.assertIsNotNone(self.store.read_journal())
                finally:
                    process.kill()
                    process.communicate(timeout=5)
                with self.lock.acquire(timeout=2):
                    target = self.store.reconciliation_target()
                    self.assertEqual(target, None if stage == "before" else release())
                    self.store.clear_after_reconciliation()
