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
import time
from pathlib import Path
from typing import Protocol

from .environment import EnvironmentError_, load_environment_file
from .layout import Layout
from .shutdown import DEFAULT_ACTION, DEFAULT_TIMEOUT_SECONDS, FORCE_EXIT_SECONDS, policy
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

    def __init__(self, message: str, phase: str, detail: str = "", *, intervention_required: bool = False,
                 kind: str = ""):
        super().__init__(message)
        self.phase = phase
        self.detail = detail
        self.intervention_required = intervention_required  # the process may still be running: nothing may restart it
        self.kind = kind


def release_id(release: dict) -> str:
    """A short stable identity for a release record, used in resource names and labels."""
    artifact = release["artifact"]
    identity = artifact.get("image") or artifact.get("sha256")
    payload = json.dumps([release["commit"], identity, release["sequence"]], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


_SIGNALS = {"HUP": 1, "INT": 2, "QUIT": 3, "USR1": 10, "USR2": 12, "TERM": 15, "ALRM": 14, "WINCH": 28}
_UNCATCHABLE = {9, 19}  # SIGKILL and SIGSTOP cannot be handled, so they are not a graceful stop
POLL_SECONDS = 0.5


def stop_signal(configured: str) -> str:
    """Validate a container's stop signal (empty means SIGTERM); only catchable signals qualify as graceful."""
    text = configured.strip()
    if text in ("", "<no value>"):
        return "SIGTERM"
    name = text[3:] if text.upper().startswith("SIG") else text
    if name.isdigit() and 1 <= int(name) <= 31 and int(name) not in _UNCATCHABLE:
        return name
    if name.upper() in _SIGNALS:
        return "SIG" + name.upper()
    raise RuntimeFailure("the container's stop signal is not one that a process can handle gracefully", "stop")


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

    def __init__(self, system: System, layout: Layout, *, platform_name: str | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.system, self.layout = system, layout
        self.clock, self.sleep = clock, sleep
        self.stop_report: list[dict] = []  # what stopping did: stages, elapsed time, policy, whether force was needed
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
        self.check_volume(registration, create=False)  # read-only: a plan or a refused deploy creates nothing
        return {"platform": image_platform, "revision_label": revision or None}

    def check_volume(self, registration: dict, *, create: bool) -> None:
        """Accept only this application's plain local volume; optionally create it (never in a read-only plan)."""
        name = self.volume_name(registration)
        inspect = self._docker(["volume", "inspect", "--format", "{{.Driver}}|{{json .Options}}|{{json .Labels}}", name],
                               "storage")
        if inspect.code != 0:
            if "no such volume" not in inspect.output.lower():
                raise RuntimeFailure("the application volume could not be inspected", "storage", inspect.output)
            if create:
                made = self._docker(["volume", "create", "--driver", "local", "--label",
                                     f"shiplord.app={registration['application_id']}", name], "storage")
                if made.code != 0:
                    raise RuntimeFailure("the application volume could not be created", "storage", made.output)
            return
        try:
            driver, options, labels = inspect.output.strip().split("|", 2)
            options, labels = json.loads(options) or {}, json.loads(labels) or {}
        except (ValueError, TypeError):
            raise RuntimeFailure("the application volume could not be understood", "storage") from None
        if driver != "local" or options or not isinstance(labels, dict) or labels.get("shiplord.app", registration["application_id"]) != registration["application_id"]:
            raise RuntimeFailure("the application volume does not match what Shiplord manages; nothing was changed", "storage")

    def _container_ids(self, registration: dict, phase: str = "stop") -> list[str]:
        listing = self._docker(["ps", "-aq", "--no-trunc", "--filter", f"label=shiplord.app={registration['application_id']}"],
                               phase)
        if listing.code != 0:
            raise RuntimeFailure("existing containers could not be listed", phase, listing.output)
        identifiers = listing.output.split()
        for identifier in identifiers:
            if len(identifier) != 64 or not all(c in "0123456789abcdef" for c in identifier):
                raise RuntimeFailure("unexpected container listing", phase)
        return identifiers

    _INSPECT = ("{{.State.Status}} {{.HostConfig.RestartPolicy.Name}} {{.Config.StopSignal}}"
                "|{{index .Config.Labels \"shiplord.shutdown_timeout\"}}|{{index .Config.Labels \"shiplord.shutdown_action\"}}")

    def _outgoing(self, identifier: str) -> dict:
        result = self._docker(["inspect", "--format", self._INSPECT, identifier], "stop")
        if result.code != 0:
            raise RuntimeFailure("an existing container could not be inspected", "stop", result.output)
        try:
            head, timeout, action = result.output.strip().split("|")
            status, restart, *signal = head.split(" ", 2)
            signal = signal[0] if signal else ""
            if status not in ("created", "running", "paused", "restarting", "removing", "exited", "dead"):
                raise ValueError
        except ValueError:
            raise RuntimeFailure("an existing container reported unusable shutdown metadata", "stop") from None
        absent = ("<no value>", "")
        if timeout in absent and action in absent:  # a container from before the policy existed
            timeout, action = str(DEFAULT_TIMEOUT_SECONDS), DEFAULT_ACTION
        if not (timeout.isdigit() and 1 <= int(timeout) <= 300 and action in ("force", "fail")):
            raise RuntimeFailure("an existing container reported unusable shutdown metadata", "stop")
        return {"id": identifier, "status": status, "restart_policy": restart, "signal": signal,
                "timeout": int(timeout), "action": action}

    def _state(self, identifier: str) -> str:
        result = self._docker(["inspect", "--format", "{{.State.Status}}", identifier], "stop")
        if result.code != 0:
            if "no such" in result.output.lower():
                return "gone"
            raise RuntimeFailure("an existing container could not be inspected", "stop", result.output)
        return result.output.strip()

    def _wait_exit(self, identifier: str, seconds: float) -> bool:
        deadline = self.clock() + seconds
        while True:
            if self._state(identifier) in ("exited", "dead", "created", "gone"):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(POLL_SECONDS)

    def _remove(self, identifier: str) -> None:
        removal = self._docker(["rm", identifier], "stop")  # no --force, no -v: only a stopped container; volumes persist
        if removal.code != 0 and self._state(identifier) != "gone":
            raise RuntimeFailure("a stopped container could not be removed", "stop", removal.output)

    def stop_in_progress(self, registration: dict) -> bool:
        """Is a container still draining after a stop attempt? Containers are created with a restart policy, and the
        stop attempt turns it off before signalling, so a live container without one is mid-shutdown."""
        for identifier in self._container_ids(registration, "inspect"):
            info = self._outgoing(identifier)
            if info["status"] in ("running", "paused", "restarting") and info["restart_policy"] == "no":
                return True
        return False

    def _remove_containers(self, registration: dict) -> None:
        """Gracefully stop whatever runs for the application (under its own recorded policy), then remove it."""
        self.stop_report = []
        outgoing = [self._outgoing(identifier) for identifier in self._container_ids(registration)]
        live = [c for c in outgoing if c["status"] in ("running", "paused", "restarting")]
        if len(live) > 1:  # nothing is signalled while it is unclear which process is the application
            raise RuntimeFailure("more than one container is running for this application", "stop",
                                 intervention_required=True, kind="ambiguous")
        for container in outgoing:
            if container["status"] in ("running", "paused", "restarting"):
                self._stop_gracefully(container)
            self._remove(container["id"])

    def _stop_gracefully(self, container: dict) -> None:
        identifier, started = container["id"], self.clock()
        report = {"timeout_seconds": container["timeout"], "action": container["action"], "stages": ["stop_requested"],
                  "forced": False}
        self.stop_report.append(report)
        signal = stop_signal(container["signal"])  # validated before anything is changed or sent
        if container["status"] == "paused":
            raise RuntimeFailure("the container is paused and cannot handle a shutdown signal", "stop",
                                 intervention_required=True, kind="paused")
        if container["restart_policy"] != "no":
            update = self._docker(["update", "--restart=no", identifier], "stop")  # first: nothing may restart it
            if update.code != 0:
                raise RuntimeFailure("the container's restart policy could not be disabled", "stop", update.output)
            sent = self._docker(["kill", "--signal", signal, identifier], "stop")
            if sent.code != 0 and self._state(identifier) not in ("exited", "dead", "gone"):
                raise RuntimeFailure("the shutdown signal could not be sent", "stop", sent.output)
            report["stages"].append("signalled")
        elif container["action"] == "fail":  # a previous worker already signalled it: never signal again, never kill
            if self._state(identifier) in ("exited", "dead", "gone"):
                report["stages"].append("exited")
                return
            self._expired(container, report, started)
        # The original deadline is not persisted, so a continuing attempt waits a full period without re-signalling.
        report["stages"].append("waiting")
        if self._wait_exit(identifier, container["timeout"]):
            report["stages"].append("exited")
            report["elapsed_seconds"] = round(self.clock() - started, 1)
            return
        if container["action"] == "fail":
            self._expired(container, report, started)
        killed = self._docker(["kill", "--signal", "KILL", identifier], "stop")
        if killed.code != 0 and self._state(identifier) not in ("exited", "dead", "gone"):
            raise RuntimeFailure("the container could not be killed after its shutdown deadline", "stop", killed.output)
        report["forced"] = True
        report["stages"].append("forced")
        if not self._wait_exit(identifier, FORCE_EXIT_SECONDS):
            raise RuntimeFailure("the container did not exit after being killed", "stop", intervention_required=True,
                                 kind="kill_unconfirmed")
        report["elapsed_seconds"] = round(self.clock() - started, 1)

    def _expired(self, container: dict, report: dict, started: float) -> None:
        report["stages"].append("deadline_expired")
        report["elapsed_seconds"] = round(self.clock() - started, 1)
        raise RuntimeFailure(f"the application did not stop within {container['timeout']} seconds and its policy is not "
                             "to force it; it was signalled and may still exit. Run `shiplord recover` once it has",
                             "stop", intervention_required=True, kind="shutdown_timeout")

    def stop(self, registration: dict) -> None:
        self._remove_containers(registration)

    def start(self, registration: dict, release: dict) -> None:
        """Replace whatever runs for this application with a container for ``release``."""
        config = release["configuration"]
        http = config["http"]
        try:
            custom = load_environment_file(self.layout.environments / registration["application_id"] / "app.env")
        except EnvironmentError_ as error:
            raise RuntimeFailure(f"the environment file is unusable: {error}", "start") from None
        self.check_volume(registration, create=True)  # before the running release is touched
        self._remove_containers(registration)
        timeout, action = policy(config)
        memory = f"{config['resources']['memory_mb']}m"
        argv = ["run", "--detach", "--name", self.container_name(registration, release),
                "--label", f"shiplord.app={registration['application_id']}",
                "--label", f"shiplord.release={release_id(release)}",
                "--label", f"shiplord.shutdown_timeout={timeout}", "--label", f"shiplord.shutdown_action={action}",
                "--restart", "unless-stopped", "--memory", memory, "--memory-swap", memory, "--pids-limit", "512",
                "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--stop-timeout", str(timeout),
                "--publish", f"127.0.0.1:{registration['host_port']}:{http['container_port']}",
                "--volume", f"{self.volume_name(registration)}:{container_path(config)}"]
        if custom is not None:
            argv += ["--env-file", str(self.layout.environments / registration["application_id"] / "app.env")]
        managed = {"HOST": "0.0.0.0", "PORT": str(http["container_port"]), "AGENT_DIR": "/app/src",
                   "AGENT_NAME": registration["application_id"], "LOG_LEVEL": "INFO", "RELOAD_AGENTS": "false",
                   "SERVE_WEB_INTERFACE": "false", "ALLOW_ORIGINS": json.dumps(http["frontend_origins"])}
        for key, value in managed.items():  # after --env-file, so the managed values win
            argv += ["--env", f"{key}={value}"]
        argv.append(release["artifact"]["image"])
        result = self._docker(argv, "start")
        if result.code != 0:
            raise RuntimeFailure("the container could not be started", "start", result.output)

    def is_running(self, registration: dict, release: dict) -> bool:
        """Is the container for exactly this release running with its image and storage mount?

        The container name does not encode configuration, so a name and a readiness answer alone could
        accept a container that still has the previous mount path. A crash-looping container reports
        Running=true while Docker waits to restart it; that is not "running" either.
        """
        result = self._docker(["inspect", "--format",
                               "{{.State.Running}} {{.State.Restarting}} {{.Config.Image}}"
                               "{{range .Mounts}} {{.Type}}:{{.Name}}:{{.Destination}}{{end}}",
                               self.container_name(registration, release)], "inspect")
        if result.code != 0:
            return False
        fields = result.output.split()
        if len(fields) < 3:
            return False
        running, restarting, image, *mounts = fields
        expected = f"volume:{self.volume_name(registration)}:{container_path(release['configuration'])}"
        return (running, restarting) == ("true", "false") and _digest(image) == _digest(release["artifact"]["image"]) \
            and mounts == [expected]

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
