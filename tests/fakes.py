"""Shared fakes and builders for engine, helper, and worker tests."""

from __future__ import annotations

from shiplord.probes import ProbeFailure
from shiplord.registry import validate_registration
from shiplord.routing import RoutingError
from shiplord.runtime import RuntimeFailure, release_id

REGISTRATION = validate_registration({
    "schema_version": 1, "application_id": "orders", "deployment_uid": 1001, "deployment_user": "ship-orders",
    "runtime": "docker", "api_domain": "orders.example.com", "host_port": 20007,
    "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096})


def configuration(**http):
    return {"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk", "runtime": "docker",
            "http": dict({"api_domain": "orders.example.com", "frontend_origins": [], "container_port": 8080,
                          "readiness_path": "/ready"}, **http),
            "resources": {"memory_mb": 512}, "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}


def request(request_id="req-1", commit="a", image="b", sequence=1000, **extra):
    value = {"request_id": request_id, "commit": commit * 40, "sequence": sequence,
             "artifact": {"kind": "docker", "image": "ghcr.io/example/orders@sha256:" + image * 64},
             "configuration": configuration()}
    value.update(extra)
    return value


def record(**kwargs):
    value = request(**kwargs)
    return {k: value[k] for k in ("commit", "sequence", "artifact", "configuration")}


class FakeRuntime:
    def __init__(self):
        self.running: set[str] = set()
        self.calls: list[tuple] = []
        self.prepare_error = None
        self.start_errors: dict[str, Exception] = {}
        self.discarded: list[str] = []

    def prepare(self, registration, release):
        self.calls.append(("prepare", release_id(release)))
        if self.prepare_error:
            raise self.prepare_error
        return {"platform": "linux/amd64", "revision_label": release["commit"]}

    def start(self, registration, release):
        rid = release_id(release)
        self.calls.append(("start", rid))
        self.running = set()
        if rid in self.start_errors:
            error = self.start_errors[rid]
            if isinstance(error, RuntimeFailure):
                raise error
            raise error
        self.running = {rid}

    def stop(self, registration):
        self.calls.append(("stop",))
        self.running = set()

    def is_running(self, registration, release):
        return release_id(release) in self.running

    def logs(self, registration, tail):
        return ""

    def discard(self, registration, release):
        self.discarded.append(release["artifact"]["image"])
        return []

    def names(self):
        return [c[0] for c in self.calls]


class FakeProbes:
    def __init__(self, runtime):
        self.runtime = runtime
        self.not_ready: dict[str, ProbeFailure] = {}
        self.routing_result = True
        self.always_ready = False

    def ready_once(self, registration, release):
        return release_id(release) in self.runtime.running and release_id(release) not in self.not_ready

    def wait_ready(self, registration, release, alive):
        rid = release_id(release)
        if rid in self.not_ready:
            raise self.not_ready[rid]
        if not alive():
            raise ProbeFailure("exited", "exited")
        return {"attempts": 1, "ready": True}

    def routing(self, registration):
        return self.routing_result

    def https(self, registration, release):
        return {"verified": False, "error": "connection_failed"}

    def dns(self, registration):
        return ["192.0.2.1"]


class FakeCaddy:
    def __init__(self):
        self.validate_calls = 0
        self.reload_calls = 0
        self.fail_reload: set[int] = set()

    def validate(self, config):
        self.validate_calls += 1

    def reload(self):
        self.reload_calls += 1
        if self.reload_calls in self.fail_reload:
            raise RoutingError("failed", "reload")
