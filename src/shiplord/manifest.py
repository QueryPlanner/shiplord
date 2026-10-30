"""Load and validate nonsecret application configuration."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from .storage import StorageError, validate_storage


class ManifestError(ValueError):
    """A manifest cannot be used safely."""


_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _table(value: object, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be a table")
    if set(value) - allowed:
        raise ManifestError(f"{name} contains unknown fields")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManifestError(f"{name} must be a nonempty string without surrounding whitespace")
    return value


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ManifestError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def validate_app_name(value: object) -> str:
    """Validate an application name before using it as a path component."""
    name = _string(value, "name")
    if len(name) > 48 or not _NAME.fullmatch(name):
        raise ManifestError("name must be a lowercase application name")
    return name


def _hostname(value: object, name: str) -> str:
    host = _string(value, name)
    if len(host) > 253 or host != host.lower() or host.endswith("."):
        raise ManifestError(f"{name} must be a lowercase DNS hostname")
    labels = host.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ManifestError(f"{name} must be a lowercase DNS hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ManifestError(f"{name} must be a DNS hostname, not an IP address")


def validate_hostname(value: object, name: str) -> str:
    """Validate a lowercase DNS hostname (never an IP address)."""
    return _hostname(value, name)


def _origin(value: object) -> str:
    origin = _string(value, "http.frontend_origins item")
    try:
        parts = urlsplit(origin)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise ManifestError("http.frontend_origins contains an invalid origin") from None
    if (parts.scheme not in {"http", "https"} or not hostname or parts.username is not None
            or parts.password is not None or parts.path or parts.query or parts.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in origin)):
        raise ManifestError("http.frontend_origins must contain HTTP(S) origins without paths or credentials")
    _hostname(hostname, "http.frontend_origins hostname")
    if port is not None and not 1 <= port <= 65535:
        raise ManifestError("http.frontend_origins contains an invalid port")
    # Reject netloc forms urlsplit accepts but which are not canonical origins.
    if parts.netloc != hostname + (f":{port}" if port is not None else ""):
        raise ManifestError("http.frontend_origins contains a noncanonical origin")
    return origin


def load_manifest(path: Path = Path("ship.toml")) -> dict:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        raise ManifestError("ship.toml was not found") from None
    except (OSError, tomllib.TOMLDecodeError):
        raise ManifestError("ship.toml could not be read as valid TOML") from None

    return validate_manifest(raw)


def validate_manifest(raw: dict) -> dict:
    """Validate parsed manifest data and return its nonsecret configuration."""

    root = _table(raw, "manifest", {"schema_version", "name", "server", "template", "runtime", "http", "resources", "deploy", "github", "storage"})
    if type(root.get("schema_version")) is not int or root["schema_version"] != 1:
        raise ManifestError("schema_version must be 1")
    name = validate_app_name(root.get("name"))
    server = _string(root.get("server"), "server")
    if len(server) > 48 or not _NAME.fullmatch(server):
        raise ManifestError("server must be a lowercase server alias")
    template = _string(root.get("template"), "template")
    if template != "google-adk":
        raise ManifestError("template must be google-adk")
    runtime = _string(root.get("runtime"), "runtime")
    if runtime not in {"docker", "native"}:
        raise ManifestError("runtime must be docker or native")

    http = _table(root.get("http"), "http", {"api_domain", "frontend_origins", "container_port", "readiness_path"})
    domain = _hostname(http.get("api_domain"), "http.api_domain")
    origins = http.get("frontend_origins")
    if not isinstance(origins, list):
        raise ManifestError("http.frontend_origins must be an explicit list")
    validated_origins = [_origin(value) for value in origins]
    if len(set(validated_origins)) != len(validated_origins):
        raise ManifestError("http.frontend_origins must not contain duplicates")
    port = _integer(http.get("container_port"), "http.container_port", 1, 65535)
    readiness = _string(http.get("readiness_path"), "http.readiness_path")
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]*", readiness) or "//" in readiness:
        raise ManifestError("http.readiness_path must be an absolute URL path")

    resources = _table(root.get("resources"), "resources", {"memory_mb"})
    memory = _integer(resources.get("memory_mb"), "resources.memory_mb", 1, 1048576)
    deploy = _table(root.get("deploy"), "deploy", {"readiness_timeout_seconds", "retain_successful_releases"})
    timeout = _integer(deploy.get("readiness_timeout_seconds"), "deploy.readiness_timeout_seconds", 1, 900)
    retain = _integer(deploy.get("retain_successful_releases"), "deploy.retain_successful_releases", 2, 2)
    # Only the current and the previous release are kept (rollback needs exactly those), so other values would mislead.
    result = {"schema_version": 1, "name": name, "server": server, "template": template, "runtime": runtime,
            "http": {"api_domain": domain, "frontend_origins": validated_origins, "container_port": port, "readiness_path": readiness},
            "resources": {"memory_mb": memory}, "deploy": {"readiness_timeout_seconds": timeout, "retain_successful_releases": retain}}
    if "storage" in root:
        if runtime != "docker":
            raise ManifestError("storage applies only to Docker applications")
        try:
            result["storage"] = validate_storage(root["storage"])
        except StorageError as error:
            raise ManifestError(str(error)) from None
    if "github" in root:
        github = _table(root["github"], "github", {"repository", "deployment_branch"})
        repository = _string(github.get("repository"), "github.repository")
        if not _REPOSITORY.fullmatch(repository) or any(part in {".", ".."} or part.startswith("-") for part in repository.split("/")):
            raise ManifestError("github.repository must be an owner/repository pair")
        branch = _string(github.get("deployment_branch"), "github.deployment_branch")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or any(x in branch for x in ("..", "//", "@{")) or branch.endswith(("/", ".", ".lock")) or any(part.startswith(".") for part in branch.split("/")):
            raise ManifestError("github.deployment_branch must be a valid branch name")
        result["github"] = {"repository": repository, "deployment_branch": branch}
    return result
