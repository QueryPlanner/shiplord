"""Unprivileged, capabilities-only prototype; it performs no host operations."""

from __future__ import annotations

import os
import signal
import sys
from typing import BinaryIO

from . import __version__
from .framing import read_frame, write_frame
from .protocol import PROTOCOL_VERSION, ProtocolError, validate_request

DEADLINE_SECONDS = 15


class HostDeadline(Exception):
    """The prototype request exceeded its overall deadline."""


def _deadline_expired(_signum: int, _frame: object) -> None:
    raise HostDeadline()


def serve(source: BinaryIO, destination: BinaryIO) -> int:
    """Handle one request after its sender closes stdin."""
    try:
        request = read_frame(source)
        if source.read(1):
            raise ProtocolError("unexpected trailing input")
        request = validate_request(request, require_compatible=False)
        if request["version"] != PROTOCOL_VERSION:
            response = {"status": "incompatible", "request_id": request["request_id"],
                        "supported_versions": [PROTOCOL_VERSION]}
            code = 4
        elif request["action"] != "capabilities":
            response = {"status": "error", "request_id": request["request_id"], "error": "unsupported_action"}
            code = 1
        elif request["commit"] is not None or request["arguments"]:
            response = {"status": "error", "request_id": request["request_id"], "error": "invalid_request"}
            code = 2
        else:
            response = {"status": "ok", "request_id": request["request_id"], "version": PROTOCOL_VERSION,
                        "helper_version": __version__, "actions": ["capabilities"]}
            code = 0
    except (ProtocolError, OSError):
        response = {"status": "error", "error": "invalid_request"}
        code = 2
    try:
        write_frame(destination, response)
        destination.flush()
    except (ProtocolError, OSError):
        return 1
    return code


def run_timed(handler) -> int:
    """Run one stdio request with a server-side deadline."""
    signal.signal(signal.SIGALRM, _deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        code = handler(sys.stdin.buffer, sys.stdout.buffer)
    except HostDeadline:
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            os.set_blocking(sys.stdout.fileno(), False)
            write_frame(sys.stdout.buffer, {"status": "error", "error": "timeout"})
            sys.stdout.buffer.flush()
        except (ProtocolError, OSError):
            try:
                sys.stdout.close()
            except OSError:
                pass
        code = 3
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return code


def main() -> None:
    raise SystemExit(run_timed(serve))


if __name__ == "__main__":
    main()
