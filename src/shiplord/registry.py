"""Root-owned application registrations and loopback port allocation.

A registration fixes an application's identity, runtime, public hostname,
allowed image repository, and loopback host port. Routine deploy requests
cannot change these values; changing them needs a separate reviewed operation.
"""

from __future__ import annotations

import os
import re
import socket
from typing import Callable

from .manifest import ManifestError, validate_app_name, validate_hostname
from .release_state import HostLock, StateError, _directory, _read, _write

PORT_RANGE = range(20000, 30000)
DEFAULT_MAX_MEMORY_MB = 4096  # the ceiling a deploy request may ask for; the administrator sets it at provisioning
MAX_REGISTRATION_BYTES = 4096
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_REPOSITORY = re.compile(r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z")
_FIELDS = {"schema_version", "application_id", "deployment_uid", "deployment_user", "runtime",
           "api_domain", "host_port", "image_repository", "max_memory_mb"}
_IDENTITY_FIELDS = _FIELDS - {"host_port"}


class RegistryError(StateError):
    """A registration is invalid, conflicts with another, or cannot be stored."""


def validate_registration(value: object, application_id: str | None = None) -> dict:
    """Validate a parsed registration; optionally require a specific application."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise RegistryError("invalid registration fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RegistryError("incompatible registration version")
    try:
        app = validate_app_name(value["application_id"])
        domain = validate_hostname(value["api_domain"], "api_domain")
    except ManifestError:
        raise RegistryError("invalid registration identity") from None
    if application_id is not None and app != application_id:
        raise RegistryError("registration does not match the application")
    uid = value["deployment_uid"]
    if type(uid) is not int or not 1 <= uid <= 2**31 - 1:
        raise RegistryError("invalid deployment UID")
    user = value["deployment_user"]
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise RegistryError("invalid deployment user")
    runtime = value["runtime"]
    repository = value["image_repository"]
    if runtime == "docker":
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise RegistryError("a Docker application needs an allowed image repository")
    elif runtime == "native":
        if repository is not None:
            raise RegistryError("a native application has no image repository")
    else:
        raise RegistryError("invalid runtime")
    port = value["host_port"]
    if type(port) is not int or port not in PORT_RANGE:
        raise RegistryError("host port is outside the managed range")
    memory = value["max_memory_mb"]
    if type(memory) is not int or not 16 <= memory <= 1048576:
        raise RegistryError("the memory ceiling must be between 16 and 1048576 MiB")
    return {"schema_version": 1, "application_id": app, "deployment_uid": uid, "deployment_user": user,
            "runtime": runtime, "api_domain": domain, "host_port": port, "image_repository": repository,
            "max_memory_mb": memory}


def port_is_free(port: int) -> bool:
    """Report whether nothing currently listens on the loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class Registry:
    """Registrations in a directory whose full path was verified by the caller."""

    def __init__(self, directory: int, lock: HostLock):
        _directory(directory)
        self.directory = directory
        self.lock = lock

    def read(self, application_id: str) -> dict | None:
        app = validate_app_name(application_id)
        value = _read(self.directory, app + ".json", missing_ok=True)
        return None if value is None else validate_registration(value, app)

    def list(self) -> list[dict]:
        """Return every registration, failing closed on anything unexpected."""
        records = []
        for name in sorted(os.listdir(self.directory)):
            if name.startswith(".tmp-"):
                continue
            if not name.endswith(".json"):
                raise RegistryError("unexpected file in the registry")
            record = self.read(name[: -len(".json")])
            if record is None:
                raise RegistryError("registration disappeared while listing")
            records.append(record)
        return records

    def register(self, application_id: str, *, deployment_uid: int, deployment_user: str, runtime: str,
                 api_domain: str, image_repository: str | None, max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
                 probe: Callable[[int], bool] = port_is_free) -> dict:
        """Create a registration, or return the identical existing one."""
        if not self.lock.held:
            raise RegistryError("host lock is required")
        app = validate_app_name(application_id)
        requested = {"schema_version": 1, "application_id": app, "deployment_uid": deployment_uid,
                     "deployment_user": deployment_user, "runtime": runtime, "api_domain": api_domain,
                     "host_port": PORT_RANGE.start, "image_repository": image_repository, "max_memory_mb": max_memory_mb}
        validate_registration(requested)
        others = [record for record in self.list() if record["application_id"] != app]
        existing = self.read(app)
        if existing is not None:
            if any(existing[field] != requested[field] for field in _IDENTITY_FIELDS):
                raise RegistryError("registration differs; changing it needs an explicit update")
            return existing
        for record in others:
            if record["api_domain"] == requested["api_domain"]:
                raise RegistryError("hostname is already registered to another application")
            if record["deployment_uid"] == deployment_uid or record["deployment_user"] == deployment_user:
                raise RegistryError("deployment account is already registered to another application")
        used = {record["host_port"] for record in others}
        for port in PORT_RANGE:
            if port not in used and probe(port):
                record = validate_registration(dict(requested, host_port=port), app)
                _write(self.directory, app + ".json", record, create=True)
                return record
        raise RegistryError("no loopback port is available")

    def unregister(self, application_id: str) -> bool:
        """Remove a registration, which disables the application's access; idempotent."""
        if not self.lock.held:
            raise RegistryError("host lock is required")
        app = validate_app_name(application_id)
        try:
            os.unlink(app + ".json", dir_fd=self.directory)
            os.fsync(self.directory)
        except FileNotFoundError:
            return False
        except OSError:
            raise RegistryError("could not remove the registration") from None
        return True
