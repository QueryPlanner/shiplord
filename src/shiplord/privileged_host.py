"""The privileged helper: reached through the dispatcher and sudo, bound to one application.

Read-only actions are answered directly. State-changing actions become idempotent jobs
that a detached worker runs, so a dropped SSH session cannot interrupt a deployment.
"""

from __future__ import annotations

import io
import sys
from typing import TYPE_CHECKING, BinaryIO, Callable

from . import __version__
from .authorization import AuthorizationError, authorize_application
from .framing import read_frame, write_frame
from .host import HostDeadline, extend_deadline, run_timed
from .jobs import MAX_PENDING_JOBS, JobError
from .native import host_architecture
from .payload import PayloadError, discard_upload, receive_payload
from .manifest import ManifestError
from .protocol import PROTOCOL_VERSION, ProtocolError, validate_request
from .release_state import StateError
from .storage import FEATURE as STORAGE_FEATURE

if TYPE_CHECKING:  # the deployment machinery is imported lazily: a capabilities probe must stay light
    from .services import AppSession, HostServices

MAX_NATIVE_BYTES = 1024 * 1024 * 1024
UPLOAD_MINIMUM_RATE = 131072  # bytes per second the upload must sustain to finish in time
UPLOAD_DEADLINE_CAP_SECONDS = 3600
ACTIONS = ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover"]
# Optional features are advertised in the same list: an older client rejects unknown response fields, but accepts any extra name.
FEATURES = [STORAGE_FEATURE]


class _UploadRejected(Exception):
    """The uploaded binary failed verification."""


_DEPLOY_ARGUMENTS = {"sequence", "artifact", "configuration", "allow_older"}


def _arguments(request: dict, allowed: set[str]) -> dict:
    arguments = request["arguments"]
    if set(arguments) - allowed:
        raise ProtocolError("unknown arguments")
    return arguments


def _deploy_request(request: dict) -> dict:
    arguments = _arguments(request, _DEPLOY_ARGUMENTS)
    if "sequence" not in arguments or "artifact" not in arguments or "configuration" not in arguments:
        raise ProtocolError("missing deployment arguments")
    allow_older = arguments.get("allow_older", False)
    if type(allow_older) is not bool:
        raise ProtocolError("allow_older must be a boolean")
    return {"commit": request["commit"], "sequence": arguments["sequence"], "artifact": arguments["artifact"],
            "configuration": arguments["configuration"], "allow_older": allow_older}


def job_view(session: AppSession, record: dict) -> dict:
    """A job record as clients see it; a job whose worker vanished is reported as interrupted."""
    state = record["state"]
    if state in ("queued", "running") and not session.services.launcher.is_active(
            session.application_id, record["request_id"]):
        state = "interrupted"
    return {"request_id": record["request_id"], "action": record["action"], "state": state, "phase": record["phase"],
            "submitted_at": record["submitted_at"], "started_at": record["started_at"],
            "finished_at": record["finished_at"], "outcome": record["outcome"]}


def _submit(session: "AppSession", request: dict, action: str, payload: dict,
            upload: Callable[[], None] | None = None, discard: Callable[[], None] | None = None) -> tuple[dict, int]:
    request_id = request["request_id"]
    # A repeated request ID returns its recorded job, whatever the outcome was (create() rejects changed content).
    existing = session.jobs.get(request_id)
    if existing is not None:
        if discard:
            discard()
        session.jobs.create(request_id, action, payload)
        return {"status": "accepted", "request_id": request_id, "job": job_view(session, existing)}, 0
    if action == "deploy":
        refusal = session.deployer().preflight({**payload, "request_id": request_id})
        if refusal is not None:
            if discard:
                discard()
            outcome = {"status": refusal.status, "exit_code": refusal.exit_code, "detail": refusal.detail}
            return ({"status": "finished", "request_id": request_id, "outcome": outcome},
                    0 if refusal.ok else refusal.exit_code)
    active = [rid for rid in session.jobs.unfinished() if session.services.launcher.is_active(session.application_id, rid)]
    if len(active) >= MAX_PENDING_JOBS:  # a job whose worker vanished does not count; only live ones do
        if discard:
            discard()
        return {"status": "error", "request_id": request_id, "error": "too_many_pending",
                "message": f"{len(active)} operations for this application are already running or queued"}, 1
    if upload:
        upload()  # stage the binary before the job exists, so a failed upload leaves no job behind
    record, created = session.jobs.create(request_id, action, payload)
    if created:
        try:
            session.services.launcher.launch(session.application_id, request_id)
        except JobError:
            session.jobs.start(request_id)
            record = session.jobs.finish(request_id, {"status": "failed", "exit_code": 1,
                                                     "detail": {"error": "launch_failed"}})
            return {"status": "error", "request_id": request_id, "error": "launch_failed",
                    "job": job_view(session, record)}, 1
    return {"status": "accepted", "request_id": request_id, "job": job_view(session, record)}, 0


def _prepare(request: dict):
    """Validate every argument without touching the host.

    Returns ``(work, expects_payload)``; ``work(session, source)`` does the host work.
    """
    action, request_id = request["action"], request["request_id"]

    def ok(result: dict) -> tuple[dict, int]:
        return {"status": "ok", "request_id": request_id, "result": result}, 0

    if action == "status":
        job_id = _arguments(request, {"job_id"}).get("job_id")
        return (lambda session, source: ok({"application": session.deployer().status(),
                                           "job": _job(session, job_id)})), False
    if action == "logs":
        tail = _arguments(request, {"tail"}).get("tail", 100)
        if type(tail) is not int or not 1 <= tail <= 1000:
            raise ProtocolError("tail must be between 1 and 1000")
        return (lambda session, source: ok({"logs": session.services.runtime_factory(
            session.services, session.registration).logs(session.registration, tail)})), False
    if action in ("plan", "deploy"):
        payload = _deploy_request(request)
        if action == "plan":
            return (lambda session, source: ok(session.deployer().plan({**payload, "request_id": request_id}))), False
        artifact = payload["artifact"]
        if isinstance(artifact, dict) and artifact.get("kind") == "native":
            return (lambda session, source: _submit_native(session, request, payload, source)), True
        return (lambda session, source: _submit(session, request, "deploy", payload)), False
    _arguments(request, set())
    if request["commit"] is not None:
        raise ProtocolError("invalid request")
    return (lambda session, source: _submit(session, request, action, {})), False


def _submit_native(session: "AppSession", request: dict, payload: dict, source: BinaryIO) -> tuple[dict, int]:
    """A native deploy: the binary follows the request frame on the same stream."""
    artifact = payload["artifact"]
    size = artifact.get("size")
    if type(size) is not int or not 1 <= size <= MAX_NATIVE_BYTES:
        raise ProtocolError("invalid artifact size")
    if artifact.get("architecture") != host_architecture():  # refuse before accepting a large upload
        return ({"status": "error", "request_id": request["request_id"], "error": "invalid_payload",
                 "message": f"this server runs {host_architecture()} binaries, not {artifact.get('architecture')}"}, 2)
    extend_deadline(min(UPLOAD_DEADLINE_CAP_SECONDS, 30 + size / UPLOAD_MINIMUM_RATE))

    def upload() -> None:
        try:
            receive_payload(source, size, artifact.get("sha256"), session.services.layout.app_state
                            / session.application_id / "staging", artifact.get("architecture", "linux-amd64"))
        except PayloadError as error:
            raise _UploadRejected(str(error)) from None

    try:
        return _submit(session, request, "deploy", payload, upload=upload, discard=lambda: discard_upload(source, size))
    except _UploadRejected as error:
        return ({"status": "error", "request_id": request["request_id"], "error": "invalid_payload",
                 "message": str(error)}, 2)


def _job(session: AppSession, job_id: str | None) -> dict | None:
    if job_id is None:
        return None
    record = session.jobs.get(job_id)
    return None if record is None else job_view(session, record)


def dispatch(request: dict, registration: dict, services_factory: Callable[[], HostServices],
             source: BinaryIO | None = None) -> tuple[dict, int]:
    source = source if source is not None else io.BytesIO(b"")
    if request["action"] == "capabilities":
        if request["commit"] is not None or request["arguments"]:
            raise ProtocolError("invalid capabilities request")
        if source.read(1):
            raise ProtocolError("unexpected trailing input")
        return {"status": "ok", "request_id": request["request_id"], "version": PROTOCOL_VERSION,
                "helper_version": __version__, "actions": ACTIONS + FEATURES}, 0
    work, expects_payload = _prepare(request)  # malformed requests are rejected before any host state is opened
    if not expects_payload and source.read(1):
        raise ProtocolError("unexpected trailing input")
    from .services import AppSession
    with AppSession(services_factory(), request["application_id"]) as session:
        return work(session, source)


def _expects_payload(request: dict) -> bool:
    """Only a native deploy may send bytes after its request frame."""
    artifact = request["arguments"].get("artifact") if request["action"] == "deploy" else None
    return isinstance(artifact, dict) and artifact.get("kind") == "native"


def _production_services() -> "HostServices":
    from .worker import production_services
    return production_services()


def serve(source: BinaryIO, destination: BinaryIO, services_factory: Callable[[], HostServices] | None = None) -> int:
    if services_factory is None:
        services_factory = _production_services
    request_id = None
    try:
        request = read_frame(source)
        request = validate_request(request, require_compatible=False)
        request_id = request["request_id"]
        if not _expects_payload(request) and source.read(1):  # refused before any authorization work
            raise ProtocolError("unexpected trailing input")
        registration = authorize_application(request["application_id"])
        if request["version"] != PROTOCOL_VERSION:
            response = {"status": "incompatible", "request_id": request_id, "supported_versions": [PROTOCOL_VERSION]}
            code = 4
        else:
            response, code = dispatch(request, registration, services_factory, source)
    except AuthorizationError:
        response, code = {"status": "error", "error": "access_denied"}, 3
    except (ProtocolError, ManifestError, OSError):
        response, code = {"status": "error", "request_id": request_id, "error": "invalid_request"}, 2
    except StateError as error:
        response, code = {"status": "error", "request_id": request_id, "error": "failed", "message": str(error)}, 1
    except HostDeadline:
        raise  # run_timed answers a deadline with its own exit code
    except Exception:  # noqa: BLE001 - anything unforeseen must not leak a traceback or host paths to the caller
        response, code = {"status": "error", "request_id": request_id, "error": "failed"}, 1
    try:
        write_frame(destination, response)
        destination.flush()
    except (ProtocolError, OSError):
        return 1
    return code


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit(2)
    raise SystemExit(run_timed(serve))


if __name__ == "__main__":
    main()
