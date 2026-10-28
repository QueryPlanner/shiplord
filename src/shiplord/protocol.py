"""Provisional JSON protocol envelope; no transport or privileged helper exists yet."""

from __future__ import annotations

import json
import re
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ACTIONS = {"capabilities", "plan", "deploy", "status", "logs", "rollback", "recover"}


class ProtocolError(ValueError):
    """A protocol message is malformed or incompatible."""


def decode_request(data: bytes) -> dict[str, Any]:
    """Decode an envelope, without treating action arguments as authorized input."""
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("protocol message exceeds the size limit")
    try:
        message = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProtocolError("invalid protocol JSON") from None
    if not isinstance(message, dict):
        raise ProtocolError("protocol request must be an object")
    if set(message) != {"version", "request_id", "action", "application_id", "commit", "arguments"}:
        raise ProtocolError("protocol request has missing or unknown fields")
    if type(message["version"]) is not int or message["version"] != PROTOCOL_VERSION:
        raise ProtocolError("incompatible protocol version")
    for field in ("request_id", "application_id"):
        value = message[field]
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ProtocolError(f"invalid {field}")
    if not isinstance(message["action"], str) or message["action"] not in _ACTIONS:
        raise ProtocolError("invalid action")
    commit = message["commit"]
    if commit is not None and (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)):
        raise ProtocolError("invalid commit")
    if not isinstance(message["arguments"], dict):
        raise ProtocolError("arguments must be an object")
    return message


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")
