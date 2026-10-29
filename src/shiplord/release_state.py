"""Durable release journal primitives; no deployment or recovery is performed."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import stat
import threading
import time
from typing import Iterator

from .manifest import ManifestError, validate_manifest
from .protocol import _invalid_constant, _unique_keys

MAX_RECORD_BYTES = 65536
MAX_REQUEST_IDS = 256
MAX_SEQUENCE = 2**40  # release ordering key: the commit's committer timestamp, in seconds
_PHASES = ("prepared", "runtime-changing", "runtime-ready", "routing-changing", "routing-ready")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE = re.compile(r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class StateError(ValueError):
    """Release state is invalid or cannot be persisted safely."""


def _directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise StateError("state directory is not trusted")


def _record_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StateError("invalid request ID")
    return value


def _release(value: object, app: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"commit", "artifact", "configuration", "sequence"}:
        raise StateError("invalid release record")
    sequence = value["sequence"]
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise StateError("invalid release sequence")
    commit, artifact = value["commit"], value["artifact"]
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit) or not isinstance(artifact, dict):
        raise StateError("invalid release record")
    kind = artifact.get("kind")
    if kind == "docker":
        if set(artifact) != {"kind", "image"} or not isinstance(artifact["image"], str) or not _IMAGE.fullmatch(artifact["image"]):
            raise StateError("invalid Docker artifact")
    elif kind == "native":
        if (set(artifact) != {"kind", "sha256", "size", "architecture"}
                or not isinstance(artifact["sha256"], str) or not _DIGEST.fullmatch(artifact["sha256"])
                or type(artifact["size"]) is not int or not 1 <= artifact["size"] <= 1024 * 1024 * 1024
                or artifact["architecture"] not in ("linux-amd64", "linux-arm64")):
            raise StateError("invalid native artifact")
    else:
        raise StateError("invalid artifact kind")
    try:
        configuration = validate_manifest(value["configuration"])
    except (ManifestError, TypeError):
        raise StateError("invalid release configuration") from None
    if configuration["name"] != app:
        raise StateError("release application does not match state")
    if configuration["runtime"] != kind:
        raise StateError("release artifact does not match the configured runtime")
    return {"commit": commit, "artifact": dict(artifact), "configuration": configuration, "sequence": sequence}


def validate_release(value: object, app: str) -> dict:
    """Validate a release record for an application; raises StateError."""
    return _release(value, app)


def _state(value: object, app: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "generation", "transaction_id", "current", "previous", "seen_requests"}:
        raise StateError("invalid committed state")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise StateError("incompatible committed state")
    generation = value["generation"]
    if type(generation) is not int or generation < 0:
        raise StateError("invalid committed generation")
    seen = value["seen_requests"]
    if not isinstance(seen, list) or len(seen) > MAX_REQUEST_IDS:
        raise StateError("invalid request history")
    for request_id in seen:
        _record_id(request_id)
    if len(set(seen)) != len(seen):
        raise StateError("invalid request history")
    if generation == 0:
        if value["transaction_id"] is not None or value["current"] is not None or value["previous"] is not None:
            raise StateError("invalid initial committed state")
    else:
        if _record_id(value["transaction_id"]) not in seen:
            raise StateError("committed request is absent from history")
        _release(value["current"], app)
        if value["previous"] is not None:
            _release(value["previous"], app)
    return value


def _journal(value: object, app: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "request_id", "phase", "prior_state", "candidate"}:
        raise StateError("invalid transaction journal")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise StateError("incompatible transaction journal")
    request_id = _record_id(value["request_id"])
    if value["phase"] not in _PHASES:
        raise StateError("invalid transaction phase")
    prior = _state(value["prior_state"], app)
    if request_id not in prior["seen_requests"] or request_id == prior["transaction_id"]:
        raise StateError("invalid transaction request history")
    _release(value["candidate"], app)
    return value


def _read(directory: int, name: str, *, missing_ok: bool = False) -> dict | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise StateError("required state file is missing") from None
    except OSError:
        raise StateError("could not open state file safely") from None
    try:
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_RECORD_BYTES):
                raise StateError("state file is not private and regular")
            data = file.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            raise StateError("state file is too large")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise StateError("state file is invalid")
        return value
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise StateError("could not read valid state") from None


def _write(directory: int, name: str, value: dict, *, create: bool) -> None:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(data) > MAX_RECORD_BYTES:
        raise StateError("state record is too large")
    temporary = ".tmp-" + secrets.token_hex(16)
    created = False
    published = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if create:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        else:
            # Validate the existing target before replacing it in a trusted directory.
            _read(directory, name)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            created = False
        published = True
        os.fsync(directory)
    except (OSError, ValueError):
        message = "state write may have been published; recovery is required" if published else "state write failed"
        raise StateError(message) from None
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                raise StateError("temporary state cleanup failed; inspect state") from None


class HostLock:
    """A stable host-wide lock file in an already trusted directory."""

    def __init__(self, directory: int):
        _directory(directory)
        self.directory = directory
        self._owner: tuple[int, int] | None = None

    @property
    def held(self) -> bool:
        return self._owner == (os.getpid(), threading.get_ident())

    @contextlib.contextmanager
    def acquire(self, timeout: float = 20) -> Iterator[None]:
        if self._owner is not None:
            raise StateError("host lock is already held")
        try:
            descriptor = os.open("host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.directory)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                os.close(descriptor)
                raise StateError("host lock is not private and regular")
        except OSError:
            raise StateError("could not open host lock") from None
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StateError("host lock is busy") from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            self._owner = (os.getpid(), threading.get_ident())
            try:
                yield
            finally:
                self._owner = None
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ReleaseState:
    """Journal state inside an app directory whose full path was verified by the caller."""

    def __init__(self, app: str, directory: int, lock: HostLock):
        from .manifest import validate_app_name
        self.app = validate_app_name(app)
        _directory(directory)
        self.directory = directory
        self.lock = lock

    def _require_lock(self) -> None:
        if not self.lock.held:
            raise StateError("host lock is required")

    def read_state(self) -> dict:
        return _state(_read(self.directory, "state.json"), self.app)

    def read_journal(self) -> dict | None:
        value = _read(self.directory, "journal.json", missing_ok=True)
        return None if value is None else _journal(value, self.app)

    def initialize(self) -> None:
        self._require_lock()
        if self.read_journal() is not None:
            raise StateError("transaction journal already exists")
        _write(self.directory, "state.json", {"schema_version": 1, "generation": 0,
               "transaction_id": None, "current": None, "previous": None, "seen_requests": []}, create=True)

    def begin(self, request_id: str, candidate: dict) -> None:
        self._require_lock()
        request_id = _record_id(request_id)
        candidate = _release(candidate, self.app)
        try:
            os.fsync(self.directory)
        except OSError:
            raise StateError("state directory could not be synced") from None
        prior = self.read_state()
        if self.read_journal() is not None:
            raise StateError("unfinished transaction requires reconciliation")
        if request_id in prior["seen_requests"]:
            raise StateError("request ID was already used")
        # A sliding window: the oldest IDs fall out so an application can never run out of transactions. The
        # committed transaction's ID is kept, because it is how a repeated, already-applied request is recognised.
        seen = [*prior["seen_requests"], request_id]
        while len(seen) > MAX_REQUEST_IDS:
            del seen[next(i for i, value in enumerate(seen) if value != prior["transaction_id"])]
        prior = dict(prior, seen_requests=seen)
        _write(self.directory, "state.json", prior, create=False)
        _write(self.directory, "journal.json", {"schema_version": 1, "request_id": request_id,
               "phase": "prepared", "prior_state": prior, "candidate": candidate}, create=True)

    def advance(self, phase: str) -> None:
        self._require_lock()
        journal = self.read_journal()
        if journal is None or self.read_state() != journal["prior_state"]:
            raise StateError("transaction cannot advance")
        index = _PHASES.index(journal["phase"])
        if index + 1 >= len(_PHASES) or _PHASES[index + 1] != phase:
            raise StateError("invalid transaction phase transition")
        journal["phase"] = phase
        _write(self.directory, "journal.json", journal, create=False)

    def record_commit(self) -> None:
        self._require_lock()
        journal = self.read_journal()
        if journal is None or journal["phase"] != "routing-ready" or self.read_state() != journal["prior_state"]:
            raise StateError("transaction is not ready to commit")
        _write(self.directory, "state.json", self._candidate_state(journal), create=False)

    def _candidate_state(self, journal: dict) -> dict:
        return {"schema_version": 1, "generation": journal["prior_state"]["generation"] + 1,
                "transaction_id": journal["request_id"], "current": journal["candidate"],
                "previous": journal["prior_state"]["current"], "seen_requests": journal["prior_state"]["seen_requests"]}

    def reconciliation_target(self) -> dict | None:
        """Return the committed release to reconcile, or fail if records disagree."""
        journal = self.read_journal()
        if journal is None:
            raise StateError("no transaction requires reconciliation")
        committed = self.read_state()
        if committed == journal["prior_state"]:
            return committed["current"]
        if committed == self._candidate_state(journal) and journal["phase"] == "routing-ready":
            return committed["current"]
        raise StateError("state and journal disagree; manual intervention is required")

    def clear_after_reconciliation(self) -> None:
        """Remove the journal only after the caller verifies runtime and routing."""
        self._require_lock()
        self.reconciliation_target()
        try:
            os.unlink("journal.json", dir_fd=self.directory)
            os.fsync(self.directory)
        except OSError:
            raise StateError("journal removal may be incomplete; inspect state") from None
