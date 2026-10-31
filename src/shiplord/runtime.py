"""Runtime adapters: how a release is prepared, started, stopped, and inspected.

Releases are immutable. Starting one always recreates its container from the
release record, so a rollback is the same operation as a deploy of an older
record. Only resources labelled for the application are ever touched.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
from pathlib import Path
from typing import Protocol

from .environment import EnvironmentError_, load_environment_file
from .layout import Layout
from .storage import container_path
from .system import CommandError, System

DOCKER = "/usr/bin/docker"
PULL_TIMEOUT_SECONDS = 900
COMMAND_TIMEOUT_SECONDS = 120
MAX_LOG_BYTES = 262144
MAX_LOG_LINES = 1000
MIN_FREE_BYTES = 1024 * 1024 * 1024  # refuse to pull when less than this is free: a full disk fails a deploy halfway
DOCKER_ROOT = Path("/var/lib/docker")
REVISION_LABEL = "org.opencontainers.image.revision"
_MACHINE_PLATFORMS = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "aarch64": "linux/arm64", "arm64": "linux/arm64"}


class RuntimeFailure(Exception):
    """A runtime step failed; ``phase`` names it and ``detail`` is capped, sanitized output."""

    def __init__(self, message: str, phase: str, detail: str = ""):
        super().__init__(message)
        self.phase = phase
        self.detail = detail


def release_id(release: dict) -> str:
    """A short stable identity for a release record, used in resource names and labels."""
    artifact = release["artifact"]
    identity = artifact.get("image") or artifact.get("sha256")
    payload = json.dumps([release["commit"], identity, release["sequence"]], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _digest(image: str) -> str:
    """The immutable part of an image reference; a registry alias or mirror must not change which image is meant."""
    return image.rpartition("@")[2]


class Runtime(Protocol):
    def prepare(self, registration: dict, release: dict) -> dict: ...
    def start(self, registration: dict, release: dict) -> None: ...
    def stop(self, registration: dict) -> None: ...
    def is_running(self, registration: dict, release: dict) -> bool: ...
    def logs(self, registration: dict, tail: int) -> str: ...
    def discard(self, registration: dict, release: dict) -> list[str]: ...


class DockerRuntime:
    """Docker via the CLI. The server's platform is derived from the host unless given."""

    def __init__(self, system: System, layout: Layout, *, platform_name: str | None = None):
        self.system, self.layout = system, layout
        self.platform = platform_name or _MACHINE_PLATFORMS.get(platform.machine().lower(), "linux/amd64")

    # naming ---------------------------------------------------------------
    @staticmethod
    def container_name(registration: dict, release: dict) -> str:
        return f"shiplord-{registration['application_id']}-{release_id(release)}"

    @staticmethod
    def volume_name(registration: dict) -> str:
        return f"shiplord-{registration['application_id']}-artifacts"

    def _docker(self, argv: list[str], phase: str, *, timeout: float = COMMAND_TIMEOUT_SECONDS,
                max_output: int = 4096):
        try:
            return self.system.run([DOCKER, *argv], timeout=timeout, max_output=max_output)
        except CommandError as error:
            raise RuntimeFailure(f"docker could not complete ({error.kind})", phase, error.output) from None

    # operations -----------------------------------------------------------
    def prepare(self, registration: dict, release: dict) -> dict:
        """Pull the image by digest and verify its platform and revision before anything is stopped."""
        image = release["artifact"]["image"]
        probe = DOCKER_ROOT if self.layout.prefix == Path("/") and DOCKER_ROOT.exists() else self.layout.var
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = MIN_FREE_BYTES  # cannot measure: let docker report its own failure
        if free < MIN_FREE_BYTES:
            raise RuntimeFailure(f"not enough free disk space to pull an image ({free // 2**20} MiB free, "
                                 f"{MIN_FREE_BYTES // 2**20} MiB needed); nothing was changed", "prepare")
        login = self.layout.registry_logins / registration["application_id"]
        config = ["--config", str(login)] if (login / "config.json").is_file() else []  # this app's read-only login
        pull = self._docker([*config, "pull", "--quiet", image], "prepare", timeout=PULL_TIMEOUT_SECONDS)
        if pull.code != 0:
            raise RuntimeFailure("the image could not be pulled", "prepare", pull.output)
        inspect = self._docker(["image", "inspect", "--format",
                                '{{.Os}}/{{.Architecture}}|{{index .Config.Labels "' + REVISION_LABEL + '"}}', image],
                               "prepare")
        if inspect.code != 0:
            raise RuntimeFailure("the pulled image could not be inspected", "prepare", inspect.output)
        image_platform, _, revision = inspect.output.strip().partition("|")
        if image_platform != self.platform:
            raise RuntimeFailure(f"the image is {image_platform}, but the server needs {self.platform}", "prepare")
        revision = "" if revision == "<no value>" else revision
        if revision and revision != release["commit"]:
            raise RuntimeFailure("the image was built from a different commit than requested", "prepare")
        volume_provenance = self.check_volume(registration, create=False)  # read-only: a plan creates nothing
        result = {"platform": image_platform, "revision_label": revision or None}
        if volume_provenance is not None:
            # The deployer persists this proof in the journal before replacing the retained container.
            result["storage_provenance"] = volume_provenance
        return result

    def check_volume(self, registration: dict, *, create: bool, provenance: dict | None = None) -> dict | None:
        """Accept this application's volume, with retained-container proof for legacy unlabelled volumes."""
        name = self.volume_name(registration)
        inspect = self._docker(["volume", "inspect", "--format",
                                "{{.Driver}}|{{json .Options}}|{{json .Labels}}|{{.CreatedAt}}|{{.Mountpoint}}", name],
                               "storage")
        if inspect.code != 0:
            if "no such volume" not in inspect.output.lower():
                raise RuntimeFailure("the application volume could not be inspected", "storage", inspect.output)
            if create:
                made = self._docker(["volume", "create", "--driver", "local", "--label",
                                     f"shiplord.app={registration['application_id']}", name], "storage")
                if made.code != 0:
                    raise RuntimeFailure("the application volume could not be created", "storage", made.output)
            return None
        try:
            driver, options, labels, created_at, mountpoint = inspect.output.strip().split("|", 4)
            options, labels = json.loads(options) or {}, json.loads(labels) or {}
        except (ValueError, TypeError):
            raise RuntimeFailure("the application volume could not be understood", "storage") from None
        if (driver != "local" or options or not isinstance(labels, dict) or not created_at
                or not mountpoint.startswith("/")):
            raise RuntimeFailure("the application volume does not match what Shiplord manages; nothing was changed", "storage")
        owner = labels.get("shiplord.app")
        if owner == registration["application_id"]:
            return None
        if owner is not None or labels:
            raise RuntimeFailure("the application volume does not match what Shiplord manages; nothing was changed", "storage")

        fingerprint = hashlib.sha256((created_at + "\0" + mountpoint).encode()).hexdigest()
        if (isinstance(provenance, dict) and provenance.get("volume_name") == name
                and provenance.get("fingerprint") == fingerprint):
            return provenance
        retained = self._legacy_volume_container(registration, name)
        if retained is None:
            raise RuntimeFailure("the unlabelled application volume has no retained Shiplord container evidence", "storage")
        return {"volume_name": name, "fingerprint": fingerprint, "container_id": retained}

    def _legacy_volume_container(self, registration: dict, volume_name: str) -> str | None:
        """Return a retained Shiplord container ID only when its labels and exact volume mount agree."""
        for identifier in self._container_ids(registration, "storage"):
            inspected = self._docker(["inspect", "--format",
                                      "{{.Name}}|{{index .Config.Labels \"shiplord.app\"}}|"
                                      "{{index .Config.Labels \"shiplord.release\"}}|"
                                      "{{range .Mounts}}{{.Type}}:{{.Name}}:{{.Destination}} {{end}}", identifier],
                                     "storage")
            if inspected.code != 0:
                raise RuntimeFailure("a retained container could not be inspected for volume provenance", "storage",
                                     inspected.output)
            try:
                name, app_label, release_label, mounts = inspected.output.strip().split("|", 3)
            except ValueError:
                raise RuntimeFailure("a retained container could not be understood for volume provenance", "storage") from None
            if (app_label == registration["application_id"]
                    and len(release_label) == 12 and all(char in "0123456789abcdef" for char in release_label)
                    and name.lstrip("/") == f"shiplord-{registration['application_id']}-{release_label}"
                    and any(mount.startswith(f"volume:{volume_name}:") for mount in mounts.split())):
                return identifier
        return None

    def _container_ids(self, registration: dict, phase: str) -> list[str]:
        listing = self._docker(["ps", "-aq", "--no-trunc", "--filter",
                                f"label=shiplord.app={registration['application_id']}"], phase)
        if listing.code != 0:
            raise RuntimeFailure("existing containers could not be listed", phase, listing.output)
        identifiers = listing.output.split()
        if any(len(identifier) != 64 or any(char not in "0123456789abcdef" for char in identifier)
               for identifier in identifiers):
            raise RuntimeFailure("unexpected container listing", phase)
        return identifiers

    def _remove_containers(self, registration: dict) -> None:
        identifiers = self._container_ids(registration, "stop")
        if identifiers:
            removal = self._docker(["rm", "--force", *identifiers], "stop")  # no -v: named volumes persist
            if removal.code != 0:
                raise RuntimeFailure("existing containers could not be removed", "stop", removal.output)

    def stop(self, registration: dict) -> None:
        self._remove_containers(registration)

    def start(self, registration: dict, release: dict) -> None:
        self._start(registration, release)

    def start_with_provenance(self, registration: dict, release: dict, provenance: dict) -> None:
        """Start a release using legacy-volume proof persisted before its retained container was removed."""
        self._start(registration, release, provenance)

    def _start(self, registration: dict, release: dict, provenance: dict | None = None) -> None:
        """Replace whatever runs for this application with a container for ``release``."""
        config = release["configuration"]
        http = config["http"]
        try:
            custom = load_environment_file(self.layout.environments / registration["application_id"] / "app.env")
        except EnvironmentError_ as error:
            raise RuntimeFailure(f"the environment file is unusable: {error}", "start") from None
        self.check_volume(registration, create=True, provenance=provenance)  # before the running release is touched
        self._remove_containers(registration)
        memory = f"{config['resources']['memory_mb']}m"
        argv = ["run", "--detach", "--name", self.container_name(registration, release),
                "--label", f"shiplord.app={registration['application_id']}",
                "--label", f"shiplord.release={release_id(release)}",
                "--label", f"shiplord.template={config['template']}",
                "--restart", "unless-stopped", "--memory", memory, "--memory-swap", memory, "--pids-limit", "512",
                "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--stop-timeout", "10",
                "--publish", f"127.0.0.1:{registration['host_port']}:{http['container_port']}",
                "--volume", f"{self.volume_name(registration)}:{container_path(config)}"]
        if custom is not None:
            argv += ["--env-file", str(self.layout.environments / registration["application_id"] / "app.env")]
        managed = {"HOST": "0.0.0.0", "PORT": str(http["container_port"]), "LOG_LEVEL": "INFO"}
        if config["template"] == "google-adk":
            managed.update(AGENT_DIR="/app/src", AGENT_NAME=registration["application_id"], RELOAD_AGENTS="false",
                           SERVE_WEB_INTERFACE="false", ALLOW_ORIGINS=json.dumps(http["frontend_origins"]))
        for key, value in managed.items():  # after --env-file, so the managed values win
            argv += ["--env", f"{key}={value}"]
        argv.append(release["artifact"]["image"])
        result = self._docker(argv, "start")
        if result.code != 0:
            raise RuntimeFailure("the container could not be started", "start", result.output)

    def is_running(self, registration: dict, release: dict) -> bool:
        """Is the container running with this release's image, profile, and storage mount?

        The container name does not encode configuration, so a name and a readiness answer alone could
        accept a container that still has the previous mount path. A crash-looping container reports
        Running=true while Docker waits to restart it; that is not "running" either.
        """
        result = self._docker(["inspect", "--format",
                               "{{.State.Running}} {{.State.Restarting}} {{.Config.Image}}"
                               "{{range .Mounts}} {{.Type}}:{{.Name}}:{{.Destination}}{{end}}|"
                               "{{index .Config.Labels \"shiplord.template\"}}",
                               self.container_name(registration, release)], "inspect")
        if result.code != 0:
            return False
        state, separator, template = result.output.strip().partition("|")
        fields = state.split()
        if not separator or len(fields) < 3:
            return False
        # Containers from helpers predating profiles have no label and use the ADK contract.
        template = "google-adk" if template in {"", "<no value>"} else template
        running, restarting, image, *mounts = fields
        expected = f"volume:{self.volume_name(registration)}:{container_path(release['configuration'])}"
        # Docker adds one anonymous mount for every VOLUME instruction in an image. Those are compatible when the
        # Shiplord-managed volume is still present exactly once at the configured path.
        return (running, restarting) == ("true", "false") and _digest(image) == _digest(release["artifact"]["image"]) \
            and mounts.count(expected) == 1 and template == release["configuration"]["template"]

    def logs(self, registration: dict, tail: int) -> str:
        tail = max(1, min(int(tail), MAX_LOG_LINES))
        listing = self._docker(["ps", "-aq", "--no-trunc", "--filter", f"label=shiplord.app={registration['application_id']}"],
                               "logs")
        identifiers = listing.output.split()
        if listing.code != 0 or not identifiers:
            return ""
        result = self._docker(["logs", "--tail", str(tail), identifiers[0]], "logs", max_output=MAX_LOG_BYTES)
        return result.output

    def discard(self, registration: dict, release: dict) -> list[str]:
        """Best-effort removal of a release's image after it left the retained set; returns warnings."""
        result = self._docker(["image", "rm", release["artifact"]["image"]], "cleanup")
        return [] if result.code == 0 else ["an old image could not be removed (it may still be in use)"]
