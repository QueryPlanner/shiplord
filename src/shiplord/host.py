"""Unprivileged, capabilities-only prototype; it performs no host operations."""

from __future__ import annotations

import sys
from typing import BinaryIO

from . import __version__
from .framing import read_frame, write_frame
from .protocol import PROTOCOL_VERSION, ProtocolError, validate_request


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


def main() -> None:
    raise SystemExit(serve(sys.stdin.buffer, sys.stdout.buffer))


if __name__ == "__main__":
    main()
