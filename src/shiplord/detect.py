"""Read hints from an application's repository to suggest ship.toml defaults (client-side, never trusted)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .manifest import ManifestError, validate_app_name

DOCKERFILES = ("Dockerfile", "Containerfile")
HEALTH_PATH_SUGGESTIONS = ("/health", "/healthz", "/ready", "/")
_MAX_BYTES = 256 * 1024
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _read_text(root: Path, name: str) -> str | None:
    """A small regular file directly in ``root``, or None. Symlinks, FIFOs, devices, and large files are ignored."""
    try:
        descriptor = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_BYTES:
            return None
        data = os.read(descriptor, _MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(descriptor)


def dockerfile(root: Path) -> str | None:
    """The name of the Dockerfile or Containerfile at the repository root, if it is a readable regular file."""
    return next((name for name in DOCKERFILES if _read_text(root, name) is not None), None)


def _instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with comments removed and backslash continuations joined."""
    lines, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        lines.append(current)
        current = ""
    if current:
        lines.append(current)
    result = []
    for line in lines:
        keyword, _, arguments = line.partition(" ")
        result.append((keyword.upper(), arguments.strip()))
    return result


def _assignments(arguments: str) -> dict[str, str]:
    """``ARG``/``ENV`` values: ``NAME=value`` pairs, or the legacy ``ENV NAME value`` form."""
    words = arguments.split()
    if words and "=" not in words[0]:
        return {words[0]: " ".join(words[1:]).strip("\"'")} if len(words) > 1 else {}
    values = {}
    for word in words:
        name, equals, value = word.partition("=")
        if equals:
            values[name] = value.strip("\"'")
    return values


def dockerfile_port(root: Path) -> int | None:
    """The TCP port the final build stage EXPOSEs first, resolving simple ARG/ENV defaults; None when unclear."""
    name = dockerfile(root)
    text = _read_text(root, name) if name else None
    if text is None:
        return None
    global_args: dict[str, str] = {}  # ARGs before the first FROM; a stage sees one only if it redeclares it
    variables: dict[str, str] = {}
    exposed: str | None = None
    in_stage = False
    for instruction, arguments in _instructions(text):
        if instruction == "FROM":  # a new stage: only the final stage's EXPOSE describes the image that runs
            variables, exposed, in_stage = {}, None, True
        elif instruction == "ARG" and not in_stage:
            global_args.update(_assignments(arguments))
        elif instruction == "ARG" and arguments.split()[:1] and "=" not in arguments.split()[0]:
            name = arguments.split()[0]
            if name in global_args:
                variables[name] = global_args[name]
        elif instruction in {"ARG", "ENV"}:
            variables.update(_assignments(arguments))
        elif instruction == "EXPOSE" and arguments:
            exposed = arguments.split()[0]  # the first port of the last EXPOSE in the stage
    if exposed is None:
        return None
    port = _VARIABLE.sub(lambda match: variables.get(match.group(1) or match.group(2), "$"), exposed)
    number, _, protocol = port.partition("/")
    if protocol.lower() not in {"", "tcp"} or not number.isdigit():
        return None
    value = int(number)
    return value if 1 <= value <= 65535 else None


def suggest_template(root: Path) -> str | None:
    """``generic`` when the repository builds its own image; otherwise no suggestion."""
    return "generic" if dockerfile(root) else None


def suggest_name(directory_name: str) -> str | None:
    """The directory name as an application name (lowercase, ``_`` and spaces as ``-``), if that is valid."""
    candidate = directory_name.strip().lower().replace("_", "-").replace(" ", "-")
    try:
        return validate_app_name(candidate)
    except ManifestError:
        return None
