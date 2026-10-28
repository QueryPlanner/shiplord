"""Idempotent, root-run host bootstrap.

Installs the helper code under a content-addressed version directory, switches
``current`` to it atomically, creates the managed directories, makes Caddy import
the managed route directory, and installs an sshd drop-in that confines
deployment accounts to the forced-command dispatcher. Re-running changes nothing
when everything is already in place.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from . import __version__
from .layout import Layout
from .release_state import HostLock
from .routing import CaddyCommands, RouteManager, RoutingError
from .system import CommandError, System

CLIENT_MODULES = frozenset({"cli.py", "client.py", "commands.py", "configure.py", "github.py", "servers.py", "template.py",
                            "transport.py", "workflow.py"})
LAUNCHERS = {
    "host_launcher.py": "shiplord.privileged_host",
    "admin_launcher.py": "shiplord.admin",
    "worker_launcher.py": "shiplord.worker",
    "dispatch_launcher.py": "shiplord.dispatch",
}
LAUNCHER_TEMPLATE = (
    "import os\nimport sys\n\n"
    "sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))\n"
    "from {module} import main\n\nmain()\n"
)
PLACEHOLDER = b"# Managed by Shiplord. Generated route files live in this directory.\n"
SSHD_DROPIN = """\
# Managed by Shiplord. Confines deployment accounts to the forced-command dispatcher.
Match User ship-*
    AuthorizedKeysFile {keys}/%u
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
"""
SSHD = "/usr/sbin/sshd"
SYSTEMCTL = "/usr/bin/systemctl"


class BootstrapError(Exception):
    """Bootstrap could not complete; the message names the failed step."""


def helper_modules(source: Path) -> list[str]:
    """Modules installed on the server: everything except client-only modules."""
    return sorted(path.name for path in source.glob("*.py") if path.name not in CLIENT_MODULES)


def _chown_root(path: Path | int) -> None:
    if os.geteuid() == 0:
        os.chown(path, 0, 0)


def _ensure_dir(path: Path, mode: int) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise BootstrapError(f"{path} exists and is not a directory")
        if info.st_uid != os.geteuid():
            raise BootstrapError(f"{path} is not owned by the installing account")
        os.chmod(path, mode)
        _chown_root(path)
    except OSError:
        raise BootstrapError(f"could not prepare {path}") from None


def _write_atomic(path: Path, data: bytes, mode: int) -> bool:
    """Write ``path`` atomically; return False without touching it if identical."""
    try:
        if path.is_symlink():
            raise BootstrapError(f"{path} is a symbolic link")
        if path.exists() and path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode:
            return False
        temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fchmod(file.fileno(), mode)
                _chown_root(file.fileno())
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        _sync_directory(path.parent)
        return True
    except OSError:
        raise BootstrapError(f"could not write {path}") from None


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _build_payload(source: Path) -> tuple[str, dict[str, bytes]]:
    """Return the version directory name and every file (relative path -> bytes) to install."""
    files: dict[str, bytes] = {}
    for name in helper_modules(source):
        files[f"shiplord/{name}"] = (source / name).read_bytes()
    for launcher, module in LAUNCHERS.items():
        files[launcher] = LAUNCHER_TEMPLATE.format(module=module).encode()
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return f"{__version__}-{digest.hexdigest()[:12]}", files


def _install_version(layout: Layout, name: str, files: dict[str, bytes]) -> bool:
    version = layout.lib / name
    marker = version / ".complete"
    if marker.exists():
        for relative, data in files.items():
            try:
                if (version / relative).read_bytes() != data:
                    raise BootstrapError(f"installed files in {version} do not match their version")
            except OSError:
                raise BootstrapError(f"installed files in {version} are incomplete") from None
        return False
    _ensure_dir(version, 0o755)
    _ensure_dir(version / "shiplord", 0o755)
    for relative, data in files.items():
        _write_atomic(version / relative, data, 0o644)
    _write_atomic(marker, b"", 0o644)
    return True


def _switch_current(layout: Layout, name: str) -> bool:
    current = layout.lib / "current"
    try:
        if current.is_symlink() and os.readlink(current) == name:
            return False
        if current.exists() and not current.is_symlink():
            raise BootstrapError(f"{current} exists and is not a symbolic link")
        link = layout.lib / f".current-{secrets.token_hex(8)}"
        os.symlink(name, link)
        os.replace(link, current)
        _sync_directory(layout.lib)
        return True
    except OSError:
        raise BootstrapError("could not switch the current helper version") from None


def _install_wrappers(layout: Layout) -> bool:
    changed = False
    wrappers = {layout.host: "host_launcher.py", layout.admin: "admin_launcher.py",
                layout.worker: "worker_launcher.py", layout.dispatch: "dispatch_launcher.py"}
    for target, launcher in wrappers.items():
        _ensure_dir(target.parent, 0o755)
        text = f'#!/bin/sh\nexec /usr/bin/python3 -I -S -B {layout.lib}/current/{launcher} "$@"\n'
        changed |= _write_atomic(target, text.encode(), 0o755)
    return changed


def _install_sshd(layout: Layout, system: System) -> bool:
    path = layout.sshd_dropin
    desired = SSHD_DROPIN.format(keys=layout.authorized_keys).encode()
    _ensure_dir(path.parent, 0o755)
    previous = path.read_bytes() if path.exists() else None
    if previous == desired:
        return False
    _write_atomic(path, desired, 0o644)
    try:
        result = system.run([SSHD, "-t"])
        if result.code != 0:
            raise BootstrapError("sshd rejected the Shiplord configuration: " + (result.output.strip() or "no output"))
        result = system.run([SYSTEMCTL, "try-reload-or-restart", "ssh.service"])
        if result.code != 0:
            raise BootstrapError("sshd could not reload the Shiplord configuration: " + (result.output.strip() or "no output"))
    except (BootstrapError, CommandError) as error:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, previous, 0o644)
        if isinstance(error, CommandError):
            raise BootstrapError("could not run sshd checks") from None
        raise
    return True


def bootstrap(layout: Layout, source: Path, system: System, caddy: CaddyCommands, *,
              lock_timeout: float = 20) -> dict:
    """Install or update the host. Safe to repeat; returns which steps changed something."""
    if layout.prefix == Path("/") and os.geteuid() != 0:
        raise BootstrapError("bootstrap must run as root")
    changed: dict[str, bool] = {}
    for directory, mode in ((layout.path("/etc/shiplord"), 0o755), (layout.registrations, 0o700),
                            (layout.environments, 0o700), (layout.authorized_keys, 0o755),
                            (layout.var, 0o755), (layout.app_state, 0o700), (layout.jobs, 0o700),
                            (layout.data, 0o755), (layout.releases, 0o755), (layout.lib, 0o755)):
        _ensure_dir(directory, mode)
    name, files = _build_payload(source)
    changed["code"] = _install_version(layout, name, files)
    changed["current"] = _switch_current(layout, name)
    changed["wrappers"] = _install_wrappers(layout)

    descriptor = os.open(layout.var, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = HostLock(descriptor)
        with lock.acquire(timeout=lock_timeout):
            ship = layout.caddy_root / "ship"
            if not layout.caddy_root.is_dir():
                raise BootstrapError("Caddy is not installed: its configuration directory is missing")
            _ensure_dir(ship, 0o755)
            changed["caddy_placeholder"] = _write_atomic(ship / "00-shiplord.caddy", PLACEHOLDER, 0o644)
            try:
                changed["caddy_import"] = RouteManager(layout.caddy_root, lock, caddy).ensure_import().changed
            except RoutingError as error:
                raise BootstrapError(f"Caddy integration failed ({error.kind}): {error}") from None
            changed["sshd"] = _install_sshd(layout, system)
    finally:
        os.close(descriptor)
    return {"version": name, "changed": changed}


def main() -> None:
    """Entry point for ``sudo python3 -I -S -c`` from an unpacked client upload: one frame out."""
    import sys

    from .framing import write_frame
    from .routing import SubprocessCaddy
    from .system import HostSystem
    try:
        result = bootstrap(Layout(), Path(__file__).resolve().parent, HostSystem(), SubprocessCaddy())
        response, code = {"status": "ok", "result": result}, 0
    except BootstrapError as error:
        response, code = {"status": "error", "message": str(error)}, 1
    write_frame(sys.stdout.buffer, response)
    sys.stdout.buffer.flush()
    raise SystemExit(code)
