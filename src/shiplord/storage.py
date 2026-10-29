"""Validation of the persistent-storage configuration of a Docker application.

Stdlib-only: the server imports this module, so the privileged helper and the client
reject the same inputs. Invalid input is refused, never normalised.
"""

from __future__ import annotations

import re

DEFAULT_CONTAINER_PATH = "/app/src/.adk"
MAX_PATH_BYTES = 4096
MAX_ID = 2147483647
FEATURE = "docker_storage_v1"  # capability a helper advertises when it can honour [storage]

_COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")
# Matched per path component, so /data/etc is allowed and /etc/data is not.
_RESERVED = ("/proc", "/sys", "/dev", "/run", "/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/root",
             "/var/run", "/var/lock")


class StorageError(ValueError):
    """A storage setting cannot be used safely."""


def validate_container_path(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or len(value.encode()) > MAX_PATH_BYTES:
        raise StorageError("storage.container_path must be an ASCII absolute directory path")
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise StorageError("storage.container_path must be a canonical absolute path other than / without a trailing slash")
    components = value[1:].split("/")
    for component in components:
        if not component or component in (".", "..") or not _COMPONENT.fullmatch(component):
            raise StorageError("storage.container_path components may contain only letters, digits, '.', '_' and '-'")
    for reserved in _RESERVED:
        reserved_parts = reserved[1:].split("/")
        if components[:len(reserved_parts)] == reserved_parts:
            raise StorageError(f"storage.container_path must not be {reserved} or inside it")
    return value


def _identifier(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ID:
        raise StorageError(f"storage.{name} must be an integer between 1 and {MAX_ID}")
    return value


def validate_storage(raw: object) -> dict:
    """Validate the ``[storage]`` table and return only the fields that were given."""
    if not isinstance(raw, dict) or set(raw) - {"container_path", "owner_uid", "owner_gid"}:
        raise StorageError("storage must be a table with only container_path, owner_uid and owner_gid")
    if "container_path" not in raw:
        raise StorageError("storage.container_path is required")
    result = {"container_path": validate_container_path(raw["container_path"])}
    if ("owner_uid" in raw) != ("owner_gid" in raw):
        raise StorageError("storage.owner_uid and storage.owner_gid must be given together")
    if "owner_uid" in raw:
        result["owner_uid"] = _identifier(raw["owner_uid"], "owner_uid")
        result["owner_gid"] = _identifier(raw["owner_gid"], "owner_gid")
    return result


def container_path(configuration: dict) -> str:
    """The effective mount path: defaults are applied here, never written into stored records."""
    return configuration.get("storage", {}).get("container_path", DEFAULT_CONTAINER_PATH)
