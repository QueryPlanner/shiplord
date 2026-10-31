"""Create a nonsecret application manifest without replacing existing files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from . import detect
from .manifest import (ManifestError, validate_app_name, validate_hostname, validate_manifest, validate_origin,
                       validate_readiness_path)

DEFAULT_CONTAINER_PORT = 8080
DEFAULT_READINESS_PATH = "/ready"
DEFAULT_MEMORY_MB = 512
# Deploys above the ceiling the administrator registered at provisioning are refused (4096 MiB by default), so the
# interactive prompt stays inside it; an explicit --memory flag is checked only by the manifest rules.
INTERACTIVE_MEMORY_RANGE = (16, 4096)
TEMPLATES = ("generic", "google-adk")
ATTEMPTS = 3


class ConfigurationError(ValueError):
    """The manifest could not be created safely."""


def create_manifest(directory: Path, name: str, server: str, api_domain: str,
                    origins: list[str], runtime: str = "docker", template: str = "google-adk", *,
                    container_port: int = DEFAULT_CONTAINER_PORT, readiness_path: str = DEFAULT_READINESS_PATH,
                    memory_mb: int = DEFAULT_MEMORY_MB) -> Path:
    config = validate_manifest({
        "schema_version": 1, "name": name, "server": server,
        "template": template, "runtime": runtime,
        "http": {"api_domain": api_domain, "frontend_origins": origins,
                 "container_port": container_port, "readiness_path": readiness_path},
        "resources": {"memory_mb": memory_mb},
        "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2},
    })
    quote = json.dumps
    origin_list = ", ".join(quote(origin) for origin in config["http"]["frontend_origins"])
    content = (
        f'schema_version = 1\nname = {quote(config["name"])}\n'
        f'server = {quote(config["server"])}\ntemplate = {quote(config["template"])}\n'
        f'runtime = {quote(config["runtime"])}\n\n'
        f'[http]\napi_domain = {quote(config["http"]["api_domain"])}\n'
        f'frontend_origins = [{origin_list}]\ncontainer_port = {config["http"]["container_port"]}\n'
        f'readiness_path = {quote(config["http"]["readiness_path"])}\n\n'
        f'[resources]\nmemory_mb = {config["resources"]["memory_mb"]}\n\n'
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


Ask = Callable[[str, "str | None"], str]


def _ask_valid(ask: Ask, say: Callable[[str], None], prompt: str, default: str | None, parse: Callable[[str], object]):
    """Ask until ``parse`` accepts the answer; give up after a few invalid answers so a script cannot loop forever."""
    for _ in range(ATTEMPTS):
        answer = ask(prompt, default).strip()
        try:
            return parse(answer)
        except (ManifestError, ConfigurationError) as error:
            say(f"  {error}")
    raise ConfigurationError("too many invalid answers; no file was created")


def _integer(low: int, high: int, name: str) -> Callable[[str], int]:
    def parse(answer: str) -> int:
        if not answer.isdigit() or not low <= int(answer) <= high:
            raise ConfigurationError(f"{name} must be a whole number from {low} to {high}")
        return int(answer)
    return parse


def collect_answers(given: dict, *, root: Path, servers: list[dict[str, str]], ask: Ask,
                    say: Callable[[str], None]) -> dict:
    """Fill every missing ``create_manifest`` argument by asking, with defaults detected from the repository.

    ``given`` holds the values from flags (None when not given). Each answer is validated as it is entered, so the
    final manifest validation in ``create_manifest`` only repeats checks that already passed.
    """
    answers = dict(given)

    if answers["server"] is None:
        if not servers:
            raise ConfigurationError("no servers are registered; run `shiplord server add <alias> --ssh <user>@<host>` "
                                     "first")
        aliases = [server["alias"] for server in servers]
        say("Servers:")
        for number, server in enumerate(servers, 1):
            say(f"  {number}. {server['alias']}  ({server['ssh']})")

        def server_choice(answer: str) -> str:
            if answer.isdigit() and 1 <= int(answer) <= len(aliases):
                return aliases[int(answer) - 1]
            if answer in aliases:
                return answer
            raise ConfigurationError("choose a listed server by number or alias")
        answers["server"] = _ask_valid(ask, say, "Server", "1" if len(aliases) == 1 else None, server_choice)

    if answers["name"] is None:
        answers["name"] = _ask_valid(ask, say, "Application name", detect.suggest_name(root.name), validate_app_name)

    if answers["template"] is None:
        def template_choice(answer: str) -> str:
            if answer not in TEMPLATES:
                raise ConfigurationError("template must be generic (your own Dockerfile) or google-adk")
            return answer
        answers["template"] = _ask_valid(ask, say, "Template (generic = your own Dockerfile, google-adk)",
                                         detect.suggest_template(root), template_choice)
    if answers["runtime"] is None:
        answers["runtime"] = "docker"  # native binaries are opt-in with --runtime native

    if answers["api_domain"] is None:
        answers["api_domain"] = _ask_valid(ask, say, "Public hostname (e.g. api.example.com)", None,
                                           lambda answer: validate_hostname(answer, "the hostname"))

    if answers["origins"] is None:
        if answers["template"] == "google-adk":  # ALLOW_ORIGINS is only passed to ADK applications
            def origins(answer: str) -> list[str]:
                values = [validate_origin(item.strip()) for item in answer.split(",") if item.strip()]
                if len(set(values)) != len(values):
                    raise ConfigurationError("frontend origins must not repeat")
                return values
            answers["origins"] = _ask_valid(ask, say, "Frontend origins (comma-separated, blank for none)", "", origins)
        else:
            answers["origins"] = []

    if answers["container_port"] is None:
        if answers["runtime"] == "docker":
            detected = detect.dockerfile_port(root)
            if detected is not None:
                say(f"Detected port {detected} from EXPOSE in {detect.dockerfile(root)}.")
            say("Shiplord sets PORT to this value; it is the port your application listens on inside the container.")
            answers["container_port"] = _ask_valid(ask, say, "Container port",
                                                   str(detected or DEFAULT_CONTAINER_PORT),
                                                   _integer(1, 65535, "the port"))
        else:
            answers["container_port"] = DEFAULT_CONTAINER_PORT

    if answers["readiness_path"] is None:
        default = DEFAULT_READINESS_PATH if answers["template"] == "google-adk" else detect.HEALTH_PATH_SUGGESTIONS[0]
        say("The server polls this path until it returns 200 before routing traffic to a new release "
            f"(common: {', '.join(detect.HEALTH_PATH_SUGGESTIONS)}).")
        answers["readiness_path"] = _ask_valid(ask, say, "Health check path", default, validate_readiness_path)

    if answers["memory_mb"] is None:
        low, high = INTERACTIVE_MEMORY_RANGE
        answers["memory_mb"] = _ask_valid(ask, say, f"Memory limit in MiB ({low}-{high})", str(DEFAULT_MEMORY_MB),
                                          _integer(low, high, "the memory limit"))
    return answers
