"""Wiring for the privileged helper and worker: host services and a per-application session."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .deploy import Deployer
from .jobs import JobStore, Launcher
from .layout import Layout
from .manifest import validate_app_name
from .native import NativeRuntime
from .probes import Probes
from .registry import Registry
from .release_state import HostLock, ReleaseState, StateError
from .routing import CaddyCommands, RouteManager
from .runtime import DockerRuntime, Runtime
from .system import System

WORKER_LOCK_TIMEOUT_SECONDS = 900


@dataclass
class HostServices:
    layout: Layout
    system: System
    launcher: Launcher
    caddy: CaddyCommands
    probes: Probes = field(default_factory=Probes)
    runtime_factory: Callable[["HostServices", dict], Runtime] = lambda services, registration: (
        NativeRuntime(services.system, services.layout) if registration["runtime"] == "native"
        else DockerRuntime(services.system, services.layout))
    lock_timeout: float = WORKER_LOCK_TIMEOUT_SECONDS


def _open(path, *, create: bool = False) -> int:
    try:
        if create:
            os.makedirs(path, mode=0o700, exist_ok=True)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:  # no path in the message: it reaches the deployment account
        raise StateError("the application is not available on this host; ask an administrator to bootstrap the "
                         "server and provision the application") from None


class AppSession:
    """Open handles for one registered application; close them by leaving the ``with`` block."""

    def __init__(self, services: HostServices, application_id: str):
        self.services = services
        self.application_id = validate_app_name(application_id)
        self._descriptors: list[int] = []

    def __enter__(self) -> "AppSession":
        layout, app = self.services.layout, self.application_id
        try:
            lock_fd = _open(layout.var)
            self._descriptors.append(lock_fd)
            self.lock = HostLock(lock_fd)
            registry_fd = _open(layout.registrations)
            self._descriptors.append(registry_fd)
            self.registration = Registry(registry_fd, self.lock).read(app)
            if self.registration is None:
                raise StateError("the application is not registered")
            state_fd = _open(layout.app_state / app)
            self._descriptors.append(state_fd)
            self.state = ReleaseState(app, state_fd, self.lock)
            jobs_fd = _open(layout.jobs / app, create=True)
            self._descriptors.append(jobs_fd)
            self.jobs = JobStore(app, jobs_fd)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        while self._descriptors:
            os.close(self._descriptors.pop())

    def deployer(self, progress: Callable[[str, dict], None] = lambda phase, info: None) -> Deployer:
        services = self.services
        return Deployer(registration=self.registration, state=self.state, probes=services.probes, progress=progress,
                        routes=RouteManager(services.layout.caddy_root, self.lock, services.caddy),
                        runtime=services.runtime_factory(services, self.registration))
