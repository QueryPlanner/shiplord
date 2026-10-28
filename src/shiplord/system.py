"""Run host commands without a shell, with a deadline and bounded captured output."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

MAX_OUTPUT_BYTES = 4096
DEFAULT_TIMEOUT_SECONDS = 60
FIXED_ENVIRONMENT = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}


@dataclass(frozen=True)
class CommandResult:
    code: int
    output: str  # capped and stripped of control characters


class CommandError(Exception):
    """The command could not be run to completion; ``kind`` is "start" or "timeout"."""

    def __init__(self, message: str, kind: str, output: str = ""):
        super().__init__(message)
        self.kind = kind
        self.output = output


class System(Protocol):
    def run(self, argv: Sequence[str], *, input: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS, max_output: int = MAX_OUTPUT_BYTES) -> CommandResult: ...


def sanitize(data: bytes | bytearray, limit: int = MAX_OUTPUT_BYTES) -> str:
    text = bytes(data[:limit]).decode("utf-8", "replace")
    return "".join(char if char == "\n" or char.isprintable() else "?" for char in text)


def run_command(argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS, input: bytes | None = None,
                env: Mapping[str, str] | None = None, cwd: str = "/",
                max_output: int = MAX_OUTPUT_BYTES) -> CommandResult:
    """Run ``argv`` directly (never through a shell) and return its capped output."""
    argv = [str(part) for part in argv]
    if not argv or any("\0" in part for part in argv):
        raise CommandError("invalid command", "start")
    try:
        process = subprocess.Popen(argv, shell=False, env=dict(env if env is not None else FIXED_ENVIRONMENT),
                                   stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                                   start_new_session=True)
    except OSError:
        raise CommandError("could not start the command", "start") from None
    kept = bytearray()
    deadline = time.monotonic() + timeout
    writer = None
    if input is not None:
        def feed() -> None:
            try:
                process.stdin.write(input)
                process.stdin.close()
            except OSError:
                pass
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError("the command timed out", "timeout", sanitize(kept, max_output))
            if select.select([process.stdout], [], [], min(0.1, remaining))[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                kept.extend(chunk[: max(0, max_output - len(kept))])
            elif process.poll() is not None and not select.select([process.stdout], [], [], 0)[0]:
                break
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise CommandError("the command timed out", "timeout", sanitize(kept, max_output)) from None
        return CommandResult(code, sanitize(kept, max_output))
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.wait()
        process.stdout.close()
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if writer is not None:
            writer.join(timeout=1)


class HostSystem:
    """The production ``System``: fixed environment, no shell."""

    def run(self, argv: Sequence[str], *, input: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS, max_output: int = MAX_OUTPUT_BYTES) -> CommandResult:
        return run_command(argv, timeout=timeout, input=input, max_output=max_output)
