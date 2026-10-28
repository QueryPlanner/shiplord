"""Runtime environment files: strict parsing, managed-key protection, and safe loading.

Values are literal. They are never evaluated by a shell, and Docker's env-file
format does not unquote them, so they are stored and passed exactly as given.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

MAX_ENVIRONMENT_BYTES = 65536
MAX_VALUE_BYTES = 8192
RESERVED = frozenset({"HOST", "PORT", "AGENT_DIR", "AGENT_NAME", "LOG_LEVEL", "RELOAD_AGENTS", "SERVE_WEB_INTERFACE",
                      "ALLOW_ORIGINS", "IMAGE"})
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class EnvironmentError_(ValueError):
    """An environment file is invalid or unsafe."""


def parse_environment(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; comments and blank lines are ignored, anything else is rejected."""
    if len(text.encode("utf-8", "replace")) > MAX_ENVIRONMENT_BYTES:
        raise EnvironmentError_("environment is too large")
    values: dict[str, str] = {}
    # Split on "\n" only, exactly as Docker's env-file reader does; str.splitlines() also splits on
    # \r, \x0b, \u2028 and others, which would let this parser and Docker disagree about a line.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not _KEY.fullmatch(key):
            raise EnvironmentError_(f"line {number} is not KEY=VALUE")
        if key in RESERVED:
            raise EnvironmentError_(f"{key} is managed by Shiplord and cannot be set")
        if key in values:
            raise EnvironmentError_(f"{key} is set more than once")
        if "\0" in value or "\r" in value or len(value.encode("utf-8", "replace")) > MAX_VALUE_BYTES:
            raise EnvironmentError_(f"the value of {key} is invalid")
        values[key] = value
    return values


def render_environment(values: dict[str, str]) -> str:
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def load_environment_file(path: Path) -> dict[str, str] | None:
    """Read a private, trusted environment file; return None when it does not exist."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise EnvironmentError_("could not open the environment file safely") from None
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_ENVIRONMENT_BYTES):
            raise EnvironmentError_("the environment file is not private to the installing account")
        try:
            return parse_environment(file.read(MAX_ENVIRONMENT_BYTES + 1).decode("utf-8"))
        except UnicodeDecodeError:
            raise EnvironmentError_("the environment file is not valid UTF-8") from None
