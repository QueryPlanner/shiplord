"""Root-only administration entry point (``sudo shiplord-admin``).

The request is one protocol frame on stdin and the response one frame on stdout. Nothing
travels in argv, which any local user can read through ``ps``, so requests may carry
secrets. Deployment accounts must never reach this entry point; it refuses callers whose
sudo identity is a registered deployment account, in addition to the sudo policy.
"""

from __future__ import annotations

import os
import sys
from typing import BinaryIO

from .framing import read_frame, write_frame
from .layout import Layout
from .manifest import ManifestError
from .protocol import ADMIN_ACTIONS, PROTOCOL_VERSION, ProtocolError, validate_request
from .serverdoctor import server_doctor
from .provision import ProvisionError, deprovision, manage_environment, manage_key, manage_registry, provision
from .registry import Registry
from .retire import make_retirer
from .release_state import HostLock, StateError
from .routing import SubprocessCaddy
from .system import HostSystem, System


class AdminDenied(Exception):
    """The caller is not allowed to administer the host."""


def parse_request(message: object) -> dict:
    request = validate_request(message, require_compatible=False, actions=ADMIN_ACTIONS)
    if request["commit"] is not None or (request["application_id"] is None) != (request["action"] == "doctor"):
        raise ProtocolError("invalid administration request")
    return request


def _check_caller(layout: Layout, lock: HostLock, environ: dict[str, str]) -> None:
    """Refuse registered deployment accounts, in addition to the sudo policy that already excludes them."""
    uid = environ.get("SUDO_UID")
    if uid is None:
        return
    descriptor = os.open(layout.registrations, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if any(str(record["deployment_uid"]) == uid for record in Registry(descriptor, lock).list()):
            raise AdminDenied("deployment accounts cannot administer the host")
    finally:
        os.close(descriptor)


def handle(request: dict, layout: Layout, system: System, lock: HostLock) -> dict:
    """Run one validated administration request; the caller holds the host lock."""
    app, args, action = request["application_id"], request["arguments"], request["action"]
    if action == "doctor":
        if args:
            raise ProtocolError("unknown arguments")
        return server_doctor(layout, system, lock)
    allowed = {"provision": {"runtime", "api_domain", "image_repository", "public_key", "key_label", "max_memory_mb"},
               "keys": {"operation", "label", "public_key"}, "environment": {"operation", "text"}, "registry": {"operation", "registry", "username", "token"},
               "deprovision": {"retire"}}[action]
    if set(args) - allowed:
        raise ProtocolError("unknown arguments")
    if action == "provision":
        if set(args) - {"max_memory_mb"} != allowed - {"max_memory_mb"}:
            raise ProtocolError("missing arguments")
        extra = {"max_memory_mb": args["max_memory_mb"]} if "max_memory_mb" in args else {}
        return provision(layout, system, lock, application_id=app, runtime=args["runtime"], **extra,
                         api_domain=args["api_domain"], image_repository=args["image_repository"],
                         public_key=args["public_key"], key_label=args["key_label"])
    if action == "keys":
        return manage_key(layout, app, args.get("operation"), label=args.get("label"),
                          public_key=args.get("public_key"))
    if action == "registry":
        return manage_registry(layout, app, args.get("operation"), registry=args.get("registry"),
                               username=args.get("username"), token=args.get("token"))
    if action == "environment":
        return manage_environment(layout, app, args.get("operation"), text=args.get("text"))
    retire = None
    if args.get("retire") is True:
        retire = make_retirer(layout, system, lock, SubprocessCaddy())
    elif "retire" in args:
        raise ProtocolError("retire must be true")
    return deprovision(layout, system, lock, app, retire=retire)


def run(message: object, layout: Layout, system: System, environ: dict[str, str]) -> tuple[dict, int]:
    request_id = None
    try:
        request = parse_request(message)
        request_id = request["request_id"]
        if request["version"] != PROTOCOL_VERSION:
            return {"status": "incompatible", "request_id": request_id, "supported_versions": [PROTOCOL_VERSION]}, 4
        descriptor = os.open(layout.var, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            lock = HostLock(descriptor)
            with lock.acquire():
                _check_caller(layout, lock, environ)
                result = handle(request, layout, system, lock)
        finally:
            os.close(descriptor)
        return {"status": "ok", "request_id": request_id, "result": result}, 0
    except AdminDenied:
        return {"status": "error", "request_id": request_id, "error": "denied"}, 3
    except (ProtocolError, ManifestError):
        return {"status": "error", "request_id": request_id, "error": "invalid_request"}, 2
    except (ProvisionError, StateError, OSError) as error:
        message = str(error) if isinstance(error, (ProvisionError, StateError)) else "host operation failed"
        return {"status": "error", "request_id": request_id, "error": "failed", "message": message}, 1


def serve(source: BinaryIO, destination: BinaryIO, layout: Layout, system: System, environ: dict[str, str]) -> int:
    try:
        message = read_frame(source)
        if source.read(1):
            raise ProtocolError("unexpected trailing input")
    except ProtocolError:
        response, code = {"status": "error", "request_id": None, "error": "invalid_request"}, 2
    else:
        response, code = run(message, layout, system, environ)
    try:
        write_frame(destination, response)
        destination.flush()
    except (ProtocolError, OSError):
        return 1
    return code


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit(2)
    if os.geteuid() != 0:
        write_frame(sys.stdout.buffer, {"status": "error", "request_id": None, "error": "denied"})
        sys.stdout.buffer.flush()
        raise SystemExit(3)
    raise SystemExit(serve(sys.stdin.buffer, sys.stdout.buffer, Layout(), HostSystem(), dict(os.environ)))


if __name__ == "__main__":
    main()
