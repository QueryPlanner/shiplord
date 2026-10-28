"""Bounded framing for provisional JSON protocol messages."""

from __future__ import annotations

import json
import re
import struct
from typing import BinaryIO

from .protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, ProtocolError, _IDENTIFIER, _invalid_constant, _unique_keys


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    try:
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise ProtocolError("truncated protocol frame")
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        raise ProtocolError("could not read protocol frame") from None
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> dict:
    """Read exactly one frame; callers manage deadlines and message sequencing."""
    size = struct.unpack(">I", _read_exact(stream, 4))[0]
    if size == 0 or size > MAX_MESSAGE_BYTES:
        raise ProtocolError("invalid protocol frame size")
    try:
        message = json.loads(_read_exact(stream, size).decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProtocolError("invalid protocol JSON") from None
    if not isinstance(message, dict):
        raise ProtocolError("protocol frame must contain an object")
    return message


def write_frame(stream: BinaryIO, message: dict) -> None:
    """Write one frame without exposing message contents in errors."""
    if not isinstance(message, dict):
        raise ProtocolError("protocol frame must contain an object")
    try:
        body = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ProtocolError("protocol message is not valid JSON") from None
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError("protocol message exceeds the size limit")
    frame = struct.pack(">I", len(body)) + body
    position = 0
    try:
        while position < len(frame):
            count = stream.write(frame[position:])
            if count is None or count <= 0:
                raise ProtocolError("could not write protocol frame")
            position += count
    except OSError:
        raise ProtocolError("could not write protocol frame") from None


def check_capabilities(response: dict, request_id: str, action: str) -> None:
    """Reject uncorrelated, incompatible or unsupported capability responses."""
    if (not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id)
            or not isinstance(response, dict) or response.get("request_id") != request_id):
        raise ProtocolError("invalid capability response correlation")
    if response.get("status") == "incompatible":
        versions = response.get("supported_versions")
        if (set(response) != {"status", "request_id", "supported_versions"}
                or not isinstance(versions, list) or not versions
                or any(type(version) is not int or version < 1 for version in versions)
                or len(set(versions)) != len(versions)):
            raise ProtocolError("invalid incompatibility response")
        raise ProtocolError("incompatible protocol version")
    if set(response) != {"status", "request_id", "version", "helper_version", "actions"} or response.get("status") != "ok":
        raise ProtocolError("invalid capability response")
    if type(response["version"]) is not int or response["version"] != PROTOCOL_VERSION:
        raise ProtocolError("incompatible protocol version")
    helper_version = response["helper_version"]
    actions = response["actions"]
    if (not isinstance(helper_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}", helper_version)
            or not isinstance(actions, list) or not actions
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item) for item in actions)):
        raise ProtocolError("invalid capability response")
    if len(set(actions)) != len(actions):
        raise ProtocolError("invalid capability response")
    if action not in actions:
        raise ProtocolError("helper does not support the requested action")
