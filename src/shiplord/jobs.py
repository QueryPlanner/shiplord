"""Durable job records and launchers for operations that must outlive an SSH session.

A job is a root-owned JSON record per application and request ID. The worker updates
its phase as it goes and stores the final outcome, so a client that disconnected can
reconnect and read the result. Launching is separate from recording: production uses
a transient systemd unit; tests and containers without systemd use a detached process.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import Protocol

from .release_state import StateError, _directory, _read, _write
from .system import CommandError, System

SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
WORKER_PATH = "/usr/local/sbin/shiplord-worker"
KEEP_JOBS = 25
MAX_RUNTIME_SECONDS = 3600  # a job that has not finished by then is stopped, releasing the host lock
MAX_PENDING_JOBS = 4  # unfinished jobs per application; further submissions are refused
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
ACTIONS = ("deploy", "rollback", "recover")
TERMINAL = ("finished",)


class JobError(StateError):
    """A job record is invalid, or the job cannot be created or launched."""


def unit_name(application_id: str, request_id: str) -> str:
    # ":" is legal in a unit name but cannot occur in an application name or request ID, so two (app, request) pairs
    # can never produce the same name, and no job name can equal a native application's "shiplord-<app>.service".
    return f"shiplord-job-{application_id}:{request_id[:24]}"


def _validate(record: object, application_id: str) -> dict:
    keys = {"schema_version", "request_id", "action", "application_id", "state", "phase", "submitted_at",
            "started_at", "finished_at", "request", "outcome"}
    if not isinstance(record, dict) or set(record) != keys:
        raise JobError("invalid job record")
    if (record["schema_version"] != 1 or record["application_id"] != application_id or record["action"] not in ACTIONS
            or record["state"] not in ("queued", "running", "finished") or not isinstance(record["request_id"], str)
            or not _ID.fullmatch(record["request_id"]) or not isinstance(record["request"], dict)):
        raise JobError("invalid job record")
    if record["state"] == "finished" and not isinstance(record["outcome"], dict):
        raise JobError("a finished job has no outcome")
    return record


class JobStore:
    """Job records for one application, in a directory whose full path the caller verified."""

    def __init__(self, application_id: str, directory: int, *, clock=time.time):
        _directory(directory)
        self.application_id, self.directory, self.clock = application_id, directory, clock

    @staticmethod
    def _name(request_id: str) -> str:
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise JobError("invalid job ID")
        return request_id + ".json"

    def get(self, request_id: str) -> dict | None:
        record = _read(self.directory, self._name(request_id), missing_ok=True)
        return None if record is None else _validate(record, self.application_id)

    def create(self, request_id: str, action: str, request: dict) -> tuple[dict, bool]:
        """Create a queued job, or return the existing record for a repeated request ID."""
        existing = self.get(request_id)
        if existing is not None:
            if existing["action"] != action or existing["request"] != request:
                raise JobError("the request ID was already used for a different request")
            return existing, False
        record = {"schema_version": 1, "request_id": request_id, "action": action,
                  "application_id": self.application_id, "state": "queued", "phase": "queued",
                  "submitted_at": int(self.clock()), "started_at": None, "finished_at": None, "request": request,
                  "outcome": None}
        _validate(record, self.application_id)
        try:
            _write(self.directory, self._name(request_id), record, create=True)
        except StateError as error:
            raise JobError(str(error)) from None
        self._prune()
        return record, True

    def _update(self, request_id: str, **changes) -> dict:
        record = self.get(request_id)
        if record is None:
            raise JobError("the job does not exist")
        if record["state"] == "finished":
            raise JobError("the job already finished")
        record.update(changes)
        _validate(record, self.application_id)
        _write(self.directory, self._name(request_id), record, create=False)
        return record

    def start(self, request_id: str) -> dict:
        return self._update(request_id, state="running", phase="starting", started_at=int(self.clock()))

    def set_phase(self, request_id: str, phase: str) -> None:
        self._update(request_id, phase=str(phase)[:64])

    def finish(self, request_id: str, outcome: dict) -> dict:
        return self._update(request_id, state="finished", phase="finished", finished_at=int(self.clock()),
                            outcome=outcome)

    def unfinished(self) -> list[str]:
        """Request IDs of jobs that are queued or running according to their records."""
        found = []
        for name in os.listdir(self.directory):
            if name.endswith(".json"):
                record = _read(self.directory, name, missing_ok=True)
                if record and record.get("state") in ("queued", "running"):
                    found.append(record["request_id"])
        return found

    def _prune(self) -> None:
        names = [name for name in os.listdir(self.directory) if name.endswith(".json")]
        if len(names) <= KEEP_JOBS:
            return
        finished = []
        for name in names:
            record = _read(self.directory, name, missing_ok=True)
            if record and record.get("state") == "finished":
                finished.append((record.get("finished_at") or 0, name))
        for _, name in sorted(finished)[: len(names) - KEEP_JOBS]:
            try:
                os.unlink(name, dir_fd=self.directory)
            except OSError:
                pass


class Launcher(Protocol):
    def launch(self, application_id: str, request_id: str) -> None: ...
    def is_active(self, application_id: str, request_id: str) -> bool: ...


class SystemdLauncher:
    """Run the worker as a transient systemd service, which survives the SSH session."""

    def __init__(self, system: System, worker: str = WORKER_PATH):
        self.system, self.worker = system, worker

    def launch(self, application_id: str, request_id: str) -> None:
        argv = [SYSTEMD_RUN, "--unit", unit_name(application_id, request_id), "--collect", "--quiet",
                f"--property=RuntimeMaxSec={MAX_RUNTIME_SECONDS}", "--description", f"Shiplord job {application_id} {request_id}", "--", self.worker, application_id,
                request_id]
        try:
            result = self.system.run(argv, timeout=30)
        except CommandError:
            raise JobError("could not start the job") from None
        if result.code != 0:
            raise JobError("systemd could not start the job")

    def is_active(self, application_id: str, request_id: str) -> bool:
        try:
            result = self.system.run([SYSTEMCTL, "is-active", unit_name(application_id, request_id) + ".service"],
                                     timeout=15)
        except CommandError:
            return False
        return result.code == 0 and result.output.strip() in ("active", "activating")


class DetachedLauncher:
    """Fallback without systemd: a detached process in its own session. For tests and containers."""

    def __init__(self, argv_prefix: list[str] | None = None):
        self.argv_prefix = argv_prefix or [sys.executable, "-I", "-m", "shiplord.worker"]
        self.processes: dict[str, subprocess.Popen] = {}

    def launch(self, application_id: str, request_id: str) -> None:
        try:
            self.processes[unit_name(application_id, request_id)] = subprocess.Popen(
                [*self.argv_prefix, application_id, request_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, cwd="/")
        except OSError:
            raise JobError("could not start the job") from None

    def is_active(self, application_id: str, request_id: str) -> bool:
        process = self.processes.get(unit_name(application_id, request_id))
        return process is not None and process.poll() is None

