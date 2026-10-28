"""Fixed host layout shared by the installer, dispatcher, and helpers.

``Layout`` maps absolute host paths under an optional prefix so that the same
code can be exercised against a temporary directory in tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .manifest import validate_app_name

LIB = "/usr/local/lib/shiplord"
HOST_PATH = "/usr/local/sbin/shiplord-host"
ADMIN_PATH = "/usr/local/sbin/shiplord-admin"
WORKER_PATH = "/usr/local/sbin/shiplord-worker"
DISPATCH_PATH = "/usr/local/libexec/shiplord-dispatch"
ETC = "/etc/shiplord"
VAR = "/var/lib/shiplord"
CADDY_ROOT = "/etc/caddy"
RELEASES = "/opt/shiplord"
SYSTEMD_UNITS = "/etc/systemd/system"
SSHD_DROPIN = "/etc/ssh/sshd_config.d/50-shiplord.conf"
SUDOERS_DIR = "/etc/sudoers.d"
USER_PREFIX = "ship-"


def deployment_user(application_id: str) -> str:
    """Return the deterministic deployment account name for an application (<= 32 chars)."""
    app = validate_app_name(application_id)
    name = USER_PREFIX + app
    if len(name) <= 32:
        return name
    return f"{USER_PREFIX}{app[:16].rstrip('-')}-{hashlib.sha256(app.encode()).hexdigest()[:8]}"


def runtime_user(application_id: str) -> str:
    """The unprivileged account a native application runs as (<= 32 characters)."""
    app = validate_app_name(application_id)
    name = "app-" + app
    if len(name) <= 32:
        return name
    return f"app-{app[:19].rstrip('-')}-{hashlib.sha256(app.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class Layout:
    prefix: Path = Path("/")

    def path(self, absolute: str) -> Path:
        return self.prefix / absolute.lstrip("/")

    @property
    def lib(self) -> Path: return self.path(LIB)
    @property
    def host(self) -> Path: return self.path(HOST_PATH)
    @property
    def admin(self) -> Path: return self.path(ADMIN_PATH)
    @property
    def worker(self) -> Path: return self.path(WORKER_PATH)
    @property
    def dispatch(self) -> Path: return self.path(DISPATCH_PATH)
    @property
    def registrations(self) -> Path: return self.path(ETC + "/apps")
    @property
    def environments(self) -> Path: return self.path(ETC + "/env")
    @property
    def registry_logins(self) -> Path: return self.path(ETC + "/docker")
    @property
    def authorized_keys(self) -> Path: return self.path(ETC + "/authorized_keys")
    @property
    def var(self) -> Path: return self.path(VAR)
    @property
    def app_state(self) -> Path: return self.path(VAR + "/apps")
    @property
    def data(self) -> Path: return self.path(VAR + "/data")
    @property
    def releases(self) -> Path: return self.path(RELEASES)
    @property
    def systemd_units(self) -> Path: return self.path(SYSTEMD_UNITS)
    @property
    def jobs(self) -> Path: return self.path(VAR + "/jobs")
    @property
    def caddy_root(self) -> Path: return self.path(CADDY_ROOT)
    @property
    def sshd_dropin(self) -> Path: return self.path(SSHD_DROPIN)
    @property
    def sudoers_dir(self) -> Path: return self.path(SUDOERS_DIR)
