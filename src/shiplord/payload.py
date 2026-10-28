"""Receive a native binary uploaded after a deploy request, into root-controlled staging.

The stream must carry exactly the declared number of bytes. The file is written to a
private temporary name, checked for its SHA-256 and ELF header, and only then published
under its content-addressed name. Nothing is ever extracted or executed here.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import BinaryIO

from .native import verify_elf
from .runtime import RuntimeFailure

MAX_PAYLOAD_BYTES = 1024 * 1024 * 1024
MIN_FREE_MARGIN_BYTES = 64 * 1024 * 1024
CHUNK = 1 << 20
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PayloadError(ValueError):
    """The upload is invalid; the message is safe to show to the client."""


STALE_UPLOAD_SECONDS = 6 * 3600  # far longer than a queued job can wait for the host lock


def purge_stale(staging: Path, *, now: float | None = None) -> None:
    """Best effort: remove uploads and partial uploads nothing consumed, so staging cannot grow without bound."""
    import time
    cutoff = (now if now is not None else time.time()) - STALE_UPLOAD_SECONDS
    try:
        for entry in staging.iterdir():
            if (entry.name.endswith(".bin") or entry.name.startswith(".upload-")) and not entry.is_symlink() \
                    and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def receive_payload(stream: BinaryIO, size: object, sha256: object, staging: Path, architecture: str) -> Path:
    if type(size) is not int or not 1 <= size <= MAX_PAYLOAD_BYTES:
        raise PayloadError("the declared payload size is invalid")
    if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
        raise PayloadError("the declared checksum is invalid")
    try:
        staging.mkdir(mode=0o700, exist_ok=True)
        if staging.is_symlink() or os.stat(staging).st_uid != os.geteuid():
            raise PayloadError("the staging directory is not trusted")
        os.chmod(staging, 0o700)
        purge_stale(staging)
        if shutil.disk_usage(staging).free < size + MIN_FREE_MARGIN_BYTES:
            raise PayloadError("not enough free disk space for the upload")
    except OSError:
        raise PayloadError("the staging directory is unavailable") from None
    temporary = staging / f".upload-{secrets.token_hex(8)}"
    digest, received, header = hashlib.sha256(), 0, b""
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            while received < size:
                chunk = stream.read(min(CHUNK, size - received))
                if not chunk:
                    raise PayloadError("the upload ended before the declared size")
                if not header:
                    header = chunk[:64]
                digest.update(chunk)
                file.write(chunk)
                received += len(chunk)
            file.flush()
            os.fsync(file.fileno())
        if stream.read(1):
            raise PayloadError("the upload is longer than the declared size")
        if digest.hexdigest() != sha256:
            raise PayloadError("the upload does not match the declared checksum")
        try:
            verify_elf(header, architecture)
        except RuntimeFailure as failure:
            raise PayloadError(str(failure)) from None
        final = staging / f"{sha256}.bin"
        os.replace(temporary, final)
        return final
    except OSError:
        raise PayloadError("could not store the upload") from None
    finally:
        temporary.unlink(missing_ok=True)


def discard_upload(stream: BinaryIO, size: int) -> None:
    """Consume and ignore a declared upload (a repeated request that needs no payload)."""
    remaining = size
    while remaining > 0:
        chunk = stream.read(min(CHUNK, remaining))
        if not chunk:
            return
        remaining -= len(chunk)
