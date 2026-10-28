"""Bounded, one-shot SSH transport for a server capabilities probe."""

from __future__ import annotations

import io
import os
import selectors
import signal
import struct
import subprocess
import time
import uuid

from .framing import check_capabilities, read_frame, write_frame
from .protocol import IncompatibleProtocol, MAX_MESSAGE_BYTES, PROTOCOL_VERSION, ProtocolError
from .servers import ssh_command

DISPATCHER = "/usr/local/libexec/shiplord-dispatch"


class TransportError(ValueError):
    """The SSH exchange could not be verified."""


def _exchange(command: list[str], payload: bytes, timeout: float) -> tuple[bytes, int]:
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise TransportError("could not start SSH") from None
    assert process.stdin is not None and process.stdout is not None
    output = bytearray()
    written = 0
    completed = False
    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE, "input")
            selector.register(process.stdout, selectors.EVENT_READ, "output")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError("SSH capabilities check timed out")
                events = selector.select(remaining)
                if not events:
                    raise TransportError("SSH capabilities check timed out")
                for key, _ in events:
                    try:
                        if key.data == "input":
                            count = os.write(key.fd, payload[written:])
                            if count == 0:
                                raise TransportError("SSH closed before receiving the request")
                            written += count
                            if written == len(payload):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                process.stdout.close()
                                continue
                            output.extend(chunk)
                            if len(output) >= 4:
                                size = struct.unpack(">I", output[:4])[0]
                                if size == 0 or size > MAX_MESSAGE_BYTES or len(output) > size + 4:
                                    raise TransportError("SSH returned an invalid response")
                    except (BlockingIOError, InterruptedError):
                        continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError("SSH capabilities check timed out")
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TransportError("SSH capabilities check timed out") from None
        completed = True
        return bytes(output), status
    except OSError:
        raise TransportError("SSH capabilities check failed") from None
    finally:
        try:
            if not completed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise TransportError("could not stop the SSH process") from None
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.stdout and not process.stdout.closed:
                process.stdout.close()


def probe_capabilities(target: str, timeout: float = 20) -> dict:
    """Check a server-level capabilities exchange, without granting app access."""
    request_id = uuid.uuid4().hex
    request = {"version": PROTOCOL_VERSION, "request_id": request_id, "action": "capabilities",
               "application_id": None, "commit": None, "arguments": {}}
    frame = io.BytesIO()
    write_frame(frame, request)
    data, status = _exchange(ssh_command(target, DISPATCHER), frame.getvalue(), timeout)
    try:
        stream = io.BytesIO(data)
        response = read_frame(stream)
        try:
            check_capabilities(response, request_id, "capabilities")
        except IncompatibleProtocol:
            if status != 4:
                raise ProtocolError("inconsistent SSH status") from None
            raise
        if status != 0:
            raise ProtocolError("inconsistent SSH status")
    except IncompatibleProtocol:
        raise
    except ProtocolError:
        raise TransportError("SSH or the server dispatcher did not return a valid capabilities response") from None
    return response
