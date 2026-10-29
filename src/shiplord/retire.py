"""Take a deployed application out of service so that it can be removed (``deprovision --retire``)."""

from __future__ import annotations

from .layout import Layout
from .native import NativeRuntime
from .provision import ProvisionError
from .release_state import HostLock
from .routing import CaddyCommands, RouteManager, RoutingError
from .runtime import DockerRuntime, Runtime, RuntimeFailure
from .system import System


def make_retirer(layout: Layout, system: System, lock: HostLock, caddy: CaddyCommands,
                 runtime_for=None):
    """Return the ``retire`` callback for ``deprovision``: stop, unroute, then discard artifacts.

    The order matters: the public route goes first, so nothing is proxied to a stopped application, and anything that
    fails stops the removal with the registration (and so the application's access) still intact.
    """
    def default_runtime(registration: dict) -> Runtime:
        return NativeRuntime(system, layout) if registration["runtime"] == "native" else DockerRuntime(system, layout)

    def retire(registration: dict, state: dict) -> None:
        runtime = (runtime_for or default_runtime)(registration)
        try:
            RouteManager(layout.caddy_root, lock, caddy).remove(registration["application_id"])
            runtime.stop(registration)
        except (RoutingError, RuntimeFailure) as failure:
            raise ProvisionError(f"could not retire the application: {failure}") from None
        for release in (state["current"], state["previous"]):
            if release is not None:
                runtime.discard(registration, release)  # best effort: warnings only

    return retire
