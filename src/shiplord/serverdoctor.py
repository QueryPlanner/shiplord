"""Read-only health report of a bootstrapped server, run as root through the admin entry point."""

from __future__ import annotations

import os
import shutil
from http.client import HTTPException
from pathlib import Path

from .installer import SSHD_DROPIN
from .layout import Layout
from .probes import http_get
from .registry import Registry
from .release_state import HostLock, ReleaseState, StateError
from .routing import _Inspection  # the same tokenizer that decides whether the managed import is present
from .runtime import DOCKER_ROOT, MIN_FREE_BYTES
from .system import CommandError, System


def _run(system: System, argv: list[str]) -> tuple[bool, str]:
    try:
        result = system.run(argv, timeout=30)
    except CommandError:
        return False, "could not run"
    return result.code == 0, result.output.strip()


def _free(path: Path) -> dict:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return {"ok": False, "path": str(path), "error": "could not measure"}
    return {"ok": free >= MIN_FREE_BYTES, "path": str(path), "free_mib": free // 2**20}


def _admin_api_reachable() -> bool:
    try:
        return http_get("127.0.0.1", 2019, "/config/", timeout=2.0, read_body=False)[0] == 200
    except (OSError, HTTPException):
        return False


def server_doctor(layout: Layout, system: System, lock: HostLock) -> dict:
    checks: dict[str, dict] = {}
    current = layout.lib / "current"
    checks["helper"] = {"ok": current.is_symlink() and layout.host.exists() and layout.worker.exists()
                        and layout.dispatch.exists(), "version": os.readlink(current) if current.is_symlink() else None}
    try:
        main = (layout.caddy_root / "Caddyfile").read_text()
        wired = len(_Inspection("", f"{layout.caddy_root}/ship/*.caddy", False).own_import_lines(main)) == 1
    except Exception:  # an unreadable or uninspectable Caddyfile is a failed check, not a crash
        wired = False
    active, _ = _run(system, ["/usr/bin/systemctl", "is-active", "--quiet", "caddy"])
    checks["caddy"] = {"ok": wired and active, "import_present": wired, "service_active": active,
                       "admin_api": _admin_api_reachable()}  # informational: deploys cannot verify routing without it
    dropin_ok = layout.sshd_dropin.is_file() and layout.sshd_dropin.read_text() == SSHD_DROPIN.format(keys=layout.authorized_keys)
    valid, _ = _run(system, ["/usr/sbin/sshd", "-t"])
    checks["sshd"] = {"ok": dropin_ok and valid, "dropin_current": dropin_ok, "config_valid": valid}
    docker, version = _run(system, ["/usr/bin/docker", "info", "--format", "{{.ServerVersion}}"])
    checks["docker"] = {"ok": docker, "version": version if docker else None}
    paths = [layout.var, layout.releases] + ([DOCKER_ROOT] if layout.prefix == Path("/") and DOCKER_ROOT.exists() else [])
    checks["disk"] = {"ok": all(entry["ok"] for entry in map(_free, paths)), "paths": [_free(path) for path in paths]}
    applications, problems = [], 0
    try:
        descriptor = os.open(layout.registrations, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            registrations = Registry(descriptor, lock).list()
        finally:
            os.close(descriptor)
    except (OSError, StateError):
        registrations, problems = [], 1
    for registration in registrations:
        app = registration["application_id"]
        entry = {"application": app, "runtime": registration["runtime"], "host_port": registration["host_port"]}
        try:
            state_fd = os.open(layout.app_state / app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                store = ReleaseState(app, state_fd, lock)
                state, journal = store.read_state(), store.read_journal()
            finally:
                os.close(state_fd)
            entry.update(ok=journal is None, deployed=state["current"] is not None,
                         unfinished_transaction=None if journal is None else journal["phase"])
        except (OSError, StateError):
            entry.update(ok=False, error="state is unreadable")
        problems += 0 if entry["ok"] else 1
        applications.append(entry)
    checks["applications"] = {"ok": problems == 0, "applications": applications}
    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}
