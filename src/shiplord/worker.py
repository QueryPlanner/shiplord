"""Runs one queued job (deploy, rollback, or recover) as root, outside any SSH session.

Started by the privileged helper as a transient service. It takes the host lock, runs
the transaction, and records the outcome in the job file so a client can read it later.
"""

from __future__ import annotations

import os
import sys

from .jobs import JobError
from .manifest import ManifestError, validate_app_name
from .release_state import StateError
from .services import AppSession, HostServices


def _outcome(status: str, exit_code: int, detail: dict) -> dict:
    return {"status": status, "exit_code": exit_code, "detail": detail}


def run_job(services: HostServices, application_id: str, request_id: str) -> int:
    """Execute the job and return its exit code; the outcome is always recorded."""
    with AppSession(services, application_id) as session:
        record = session.jobs.get(request_id)
        if record is None:
            return 2
        if record["state"] != "queued":
            return 0  # a duplicate worker, or a job that already ran
        session.jobs.start(request_id)
        outcome = None
        try:
            with session.lock.acquire(timeout=services.lock_timeout):
                deployer = session.deployer(lambda phase, info: session.jobs.set_phase(request_id, phase))
                if record["action"] == "deploy":
                    result = deployer.deploy({**record["request"], "request_id": request_id})
                elif record["action"] == "rollback":
                    result = deployer.rollback(request_id)
                else:
                    result = deployer.recover()
                outcome = _outcome(result.status, result.exit_code, result.detail)
        except StateError as error:
            if "host lock is busy" in str(error):
                outcome = _outcome("failed", 1, {"error": "host_busy", "message": "another operation holds the host lock"})
            else:
                outcome = _outcome("failed", 1, {"error": "state_error", "message": str(error)})
        except Exception as error:  # the transaction may be half done: say so rather than claim success
            unfinished = _journal_present(session)
            outcome = _outcome("intervention_required" if unfinished else "failed", 6 if unfinished else 1,
                               {"error": "worker_error", "message": type(error).__name__})
        session.jobs.finish(request_id, outcome)
        return outcome["exit_code"]


def _journal_present(session: AppSession) -> bool:
    try:
        return session.state.read_journal() is not None
    except StateError:
        return True


def production_services() -> HostServices:
    from .layout import Layout
    from .jobs import SystemdLauncher
    from .routing import SubprocessCaddy
    from .system import HostSystem
    system = HostSystem()
    return HostServices(Layout(), system, SystemdLauncher(system), SubprocessCaddy())


def main() -> None:
    if len(sys.argv) != 3 or os.geteuid() != 0:
        raise SystemExit(2)
    try:
        application_id, request_id = validate_app_name(sys.argv[1]), sys.argv[2]
        raise SystemExit(run_job(production_services(), application_id, request_id))
    except (ManifestError, JobError, StateError):
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
