"""Native Linux binary runtime: a confined systemd service per application.

Binaries are root-owned and versioned under ``/opt/shiplord/<app>/<release>/app``; the service
runs as a separate unprivileged account with its writable data kept outside release directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

from .environment import EnvironmentError_, load_environment_file
from .layout import Layout, runtime_user
from .runtime import RuntimeFailure, release_id
from .system import CommandError, System

SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
MAX_LOG_BYTES = 262144
MAX_LOG_LINES = 1000
MARKER = re.compile(r"^# shiplord-release: ([0-9a-f]{12})$", re.M)
_RELEASE_DIRECTORY = re.compile(r"[0-9a-f]{12}\Z")
ARCHITECTURES = {"linux-amd64": 0x3E, "linux-arm64": 0xB7}  # ELF e_machine values
_HOST_MACHINES = {"x86_64": "linux-amd64", "amd64": "linux-amd64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}


def host_architecture() -> str:
    """The architecture this server runs binaries for."""
    import platform
    return _HOST_MACHINES.get(platform.machine().lower(), "linux-amd64")


def elf_architecture(header: bytes) -> str:
    """The architecture a valid ELF executable header is built for."""
    verify_elf(header, None)
    machine = int.from_bytes(header[18:20], "little")
    for name, value in ARCHITECTURES.items():
        if value == machine:
            return name
    raise RuntimeFailure("the binary is for a CPU architecture Shiplord does not support (amd64 and arm64 are)", "prepare")


def verify_elf(header: bytes, architecture: str | None = "linux-amd64") -> None:
    """Accept only a 64-bit little-endian executable or PIE ELF; if given, for exactly that architecture."""
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise RuntimeFailure("the payload is not a Linux ELF executable (macOS and Windows binaries cannot run here)", "prepare")
    if header[4] != 2 or header[5] != 1:
        raise RuntimeFailure("the binary is not 64-bit little-endian", "prepare")
    file_type, machine = int.from_bytes(header[16:18], "little"), int.from_bytes(header[18:20], "little")
    if file_type not in (2, 3):
        raise RuntimeFailure("the ELF file is not an executable", "prepare")
    if architecture is not None and machine != ARCHITECTURES[architecture]:
        raise RuntimeFailure(f"the binary is for another CPU architecture; this server needs {architecture}", "prepare")


def systemd_quote(value: str) -> str:
    """Quote a value for a systemd EnvironmentFile, which (unlike Docker's format) unquotes and unescapes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_environment(registration: dict, release: dict, custom: dict[str, str] | None) -> str:
    http = release["configuration"]["http"]
    managed = {"HOST": "127.0.0.1", "PORT": str(registration["host_port"]), "AGENT_NAME": registration["application_id"],
               "LOG_LEVEL": "INFO", "RELOAD_AGENTS": "false", "SERVE_WEB_INTERFACE": "false",
               "ALLOW_ORIGINS": json.dumps(http["frontend_origins"])}
    values = {**(custom or {}), **managed}  # managed keys always win; the parser already forbids overriding them
    return "".join(f"{key}={systemd_quote(values[key])}\n" for key in sorted(values))


def render_unit(layout: Layout, registration: dict, release: dict) -> str:
    app = registration["application_id"]
    user = runtime_user(app)
    data = layout.data / app
    return f"""# Managed by Shiplord. Do not edit.
# shiplord-release: {release_id(release)}
[Unit]
Description=Shiplord application {app}
After=network.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={data}
ExecStart={layout.releases / app / release_id(release) / "app"}
EnvironmentFile={layout.environments / app / "native.env"}
Restart=always
RestartSec=2
MemoryMax={release["configuration"]["resources"]["memory_mb"]}M
MemorySwapMax=0
TasksMax=512
LimitNOFILE=65536
TimeoutStopSec=15
KillMode=control-group
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=
SocketBindDeny=any
SocketBindAllow=tcp:{registration["host_port"]}
ReadWritePaths={data}

[Install]
WantedBy=multi-user.target
"""


def _sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class NativeRuntime:
    def __init__(self, system: System, layout: Layout):
        self.system, self.layout = system, layout

    # paths ----------------------------------------------------------------
    def unit_name(self, registration: dict) -> str:
        return f"shiplord-{registration['application_id']}.service"

    def unit_path(self, registration: dict) -> Path:
        return self.layout.systemd_units / self.unit_name(registration)

    def release_binary(self, registration: dict, release: dict) -> Path:
        return self.layout.releases / registration["application_id"] / release_id(release) / "app"

    def staged_path(self, registration: dict, release: dict) -> Path:
        return self.layout.app_state / registration["application_id"] / "staging" / (release["artifact"]["sha256"] + ".bin")

    def _systemctl(self, argv: list[str], phase: str, *, max_output: int = 4096):
        try:
            return self.system.run([SYSTEMCTL, *argv], timeout=60, max_output=max_output)
        except CommandError as error:
            raise RuntimeFailure(f"systemctl could not complete ({error.kind})", phase, error.output) from None

    # operations -------------------------------------------------------------
    def _installed(self, registration: dict, release: dict) -> bool:
        binary = self.release_binary(registration, release)
        try:
            digest, size = _sha256(binary)
        except OSError:
            return False
        artifact = release["artifact"]
        return digest == artifact["sha256"] and size == artifact["size"]

    def prepare(self, registration: dict, release: dict) -> dict:
        """Install the verified binary into its release directory; the running service is untouched."""
        artifact = release["artifact"]
        if artifact["architecture"] != host_architecture():
            raise RuntimeFailure(f"this server runs {host_architecture()} binaries, not {artifact['architecture']}", "prepare")
        if self._installed(registration, release):
            self.forget_upload(registration, release)  # a repeated upload of what is already installed
            return {"architecture": artifact["architecture"], "installed": "already"}
        staged = self.staged_path(registration, release)
        try:
            digest, size = _sha256(staged)
            with staged.open("rb") as file:
                header = file.read(64)
        except OSError:
            raise RuntimeFailure("the uploaded binary is not on the server; send it again with --binary", "prepare") from None
        if digest != artifact["sha256"] or size != artifact["size"]:
            raise RuntimeFailure("the staged binary does not match the requested checksum or size", "prepare")
        verify_elf(header, artifact["architecture"])
        target = self.release_binary(registration, release)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(target.parent, 0o755)
            temporary = target.parent / f".app.tmp-{secrets.token_hex(6)}"
            shutil.copyfile(staged, temporary)
            os.chmod(temporary, 0o755)
            if os.geteuid() == 0:
                os.chown(temporary, 0, 0)
            with temporary.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(temporary, target)
            staged.unlink(missing_ok=True)
        except OSError:
            raise RuntimeFailure("could not install the binary", "prepare") from None
        return {"architecture": artifact["architecture"], "installed": "new"}

    def forget_upload(self, registration: dict, release: dict) -> None:
        """Remove an uploaded binary that will not be installed."""
        self.staged_path(registration, release).unlink(missing_ok=True)

    def start(self, registration: dict, release: dict) -> None:
        app = registration["application_id"]
        if not self._installed(registration, release):
            raise RuntimeFailure("the release binary is missing or does not match its checksum", "start")
        try:
            custom = load_environment_file(self.layout.environments / app / "app.env")
        except EnvironmentError_ as error:
            raise RuntimeFailure(f"the environment file is unusable: {error}", "start") from None
        self._write(self.layout.environments / app / "native.env", render_environment(registration, release, custom), 0o600)
        changed = self._write(self.unit_path(registration), render_unit(self.layout, registration, release), 0o644)
        unit = self.unit_name(registration)
        # A crash-looping release trips systemd's start-rate limit and leaves the unit "failed"; without a
        # reset the next restart (for example the rollback that follows) would be refused.
        steps = ([["daemon-reload"]] if changed else []) + [["enable", unit], ["reset-failed", unit], ["restart", unit]]
        for argv in steps:
            result = self._systemctl(argv, "start")
            if result.code != 0 and argv[0] != "reset-failed":  # reset-failed errors when the unit was never loaded
                raise RuntimeFailure(f"systemctl {argv[0]} failed", "start", result.output)

    def stop(self, registration: dict) -> None:
        if not self.unit_path(registration).exists():
            return
        result = self._systemctl(["disable", "--now", self.unit_name(registration)], "stop")
        if result.code != 0:
            raise RuntimeFailure("the service could not be stopped", "stop", result.output)

    def is_running(self, registration: dict, release: dict) -> bool:
        try:
            marker = MARKER.search(self.unit_path(registration).read_text())
        except OSError:
            return False
        if not marker or marker.group(1) != release_id(release):
            return False
        result = self._systemctl(["show", "--property=ActiveState,SubState", self.unit_name(registration)], "inspect")
        state = dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)
        return result.code == 0 and state.get("ActiveState") == "active" and state.get("SubState") == "running"

    def logs(self, registration: dict, tail: int) -> str:
        tail = max(1, min(int(tail), MAX_LOG_LINES))
        try:
            result = self.system.run([JOURNALCTL, "--unit", self.unit_name(registration), "--lines", str(tail),
                                      "--no-pager", "--output", "cat"], timeout=30, max_output=MAX_LOG_BYTES)
        except CommandError:
            return ""
        return result.output if result.code == 0 else ""

    def discard(self, registration: dict, release: dict) -> list[str]:
        directory = self.release_binary(registration, release).parent
        if not _RELEASE_DIRECTORY.fullmatch(directory.name) or directory.is_symlink():
            return ["refused to remove an unexpected release directory"]
        try:
            self.staged_path(registration, release).unlink(missing_ok=True)  # an upload that was never installed
            if directory.exists():
                shutil.rmtree(directory)
        except OSError:
            return ["an old release directory could not be removed"]
        return []

    @staticmethod
    def _write(path: Path, text: str, mode: int) -> bool:
        data = text.encode()
        try:
            if path.is_file() and not path.is_symlink() and path.read_bytes() == data \
                    and stat.S_IMODE(path.stat().st_mode) == mode:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(6)}")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fchmod(file.fileno(), mode)
                if os.geteuid() == 0:
                    os.fchown(file.fileno(), 0, 0)
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except OSError:
            raise RuntimeFailure(f"could not write {path.name}", "start") from None
        return True
