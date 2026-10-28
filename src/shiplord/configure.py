"""Create a nonsecret application manifest without replacing existing files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .manifest import validate_manifest


class ConfigurationError(ValueError):
    """The manifest could not be created safely."""


def create_manifest(directory: Path, name: str, server: str, api_domain: str,
                    origins: list[str], runtime: str = "docker") -> Path:
    config = validate_manifest({
        "schema_version": 1, "name": name, "server": server,
        "template": "google-adk", "runtime": runtime,
        "http": {"api_domain": api_domain, "frontend_origins": origins,
                 "container_port": 8080, "readiness_path": "/ready"},
        "resources": {"memory_mb": 512},
        "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2},
    })
    quote = json.dumps
    origin_list = ", ".join(quote(origin) for origin in config["http"]["frontend_origins"])
    content = (
        f'schema_version = 1\nname = {quote(config["name"])}\n'
        f'server = {quote(config["server"])}\ntemplate = "google-adk"\n'
        f'runtime = {quote(config["runtime"])}\n\n'
        f'[http]\napi_domain = {quote(config["http"]["api_domain"])}\n'
        f'frontend_origins = [{origin_list}]\ncontainer_port = 8080\n'
        'readiness_path = "/ready"\n\n[resources]\nmemory_mb = 512\n\n'
        '[deploy]\nreadiness_timeout_seconds = 90\nretain_successful_releases = 2\n'
    )
    destination = directory / "ship.toml"
    try:
        # A same-directory hard link publishes the complete file atomically and
        # fails if another process created the destination first.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".shiplord-", delete=True) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.link(temporary.name, destination)
    except FileExistsError:
        raise ConfigurationError("ship.toml already exists; no file was replaced") from None
    except OSError:
        raise ConfigurationError("could not create ship.toml") from None
    return destination
