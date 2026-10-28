"""App-bound, capabilities-only helper prototype; performs no host mutation."""

from __future__ import annotations

import sys
from typing import BinaryIO

from . import __version__
from .authorization import AuthorizationError, authorize_application
from .framing import read_frame, write_frame
from .host import run_timed
from .protocol import PROTOCOL_VERSION, ProtocolError, validate_request


def serve(source: BinaryIO, destination: BinaryIO) -> int:
    try:
        request = read_frame(source)
        if source.read(1):
            raise ProtocolError("unexpected trailing input")
        request = validate_request(request, require_compatible=False)
        authorize_application(request["application_id"])
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
    except AuthorizationError:
        response = {"status": "error", "error": "access_denied"}
        code = 3
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
    if len(sys.argv) != 1:
        raise SystemExit(2)
    raise SystemExit(run_timed(serve))


if __name__ == "__main__":
    main()
