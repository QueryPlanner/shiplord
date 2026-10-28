"""The deploy engine against a real Docker daemon and a local registry (set SHIPLORD_TEST_DOCKER=1).

Only resources named for a unique test application are created, and all are removed afterwards.
Fixture images imitate the template: /ready, managed environment variables, an artifacts volume,
an unhealthy release, and a crashing release.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeCaddy  # noqa: E402
from shiplord import registry, release_state  # noqa: E402
from shiplord.deploy import Deployer, identity  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.probes import Probes  # noqa: E402
from shiplord.registry import PORT_RANGE, port_is_free, validate_registration  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState  # noqa: E402
from shiplord.routing import RouteManager  # noqa: E402
from shiplord.runtime import DockerRuntime, release_id  # noqa: E402
from shiplord.system import HostSystem  # noqa: E402

ENABLED = os.environ.get("SHIPLORD_TEST_DOCKER") == "1" and shutil.which("docker")
REGISTRY_PORT = 5000
REPOSITORY = f"localhost:{REGISTRY_PORT}/shiplord/fixture"
SERVER = '''
import http.server, json, os, sys

if os.environ.get("MODE") == "crash":
    print("fixture crashing on purpose", flush=True)
    sys.exit(1)
ARTIFACTS = "/app/src/.adk"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = 200, b"ok"
        if self.path == "/ready":
            status = 503 if os.environ.get("MODE") == "unhealthy" else 200
        elif self.path == "/version":
            body = os.environ["VERSION"].encode()
        elif self.path == "/whoami":
            body = str(os.getuid()).encode()
        elif self.path == "/env":
            keys = ("ALLOW_ORIGINS", "SERVE_WEB_INTERFACE", "RELOAD_AGENTS", "AGENT_NAME", "HOST", "PORT", "AGENT_DIR", "CUSTOM_TOKEN")
            body = json.dumps({k: os.environ.get(k) for k in keys}).encode()
        elif self.path == "/counter":
            path = os.path.join(ARTIFACTS, "counter")
            count = int(open(path).read()) + 1 if os.path.exists(path) else 1
            open(path, "w").write(str(count))
            body = str(count).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

print("fixture started version=" + os.environ["VERSION"], flush=True)
http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()
'''
DOCKERFILE = '''FROM python:3.12-slim
RUN useradd -u 1000 -m app && mkdir -p /app/src/.adk && chown -R app:app /app
USER app
ARG VERSION
ARG MODE=healthy
ARG REVISION
ENV VERSION=$VERSION MODE=$MODE
LABEL org.opencontainers.image.revision=$REVISION
COPY server.py /app/server.py
CMD ["python", "/app/server.py"]
'''
IMAGE_PATTERN = re.compile(rf"localhost:{REGISTRY_PORT}/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{{64}}\Z")
REPOSITORY_PATTERN = re.compile(rf"localhost:{REGISTRY_PORT}/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z")


def docker(*argv, check=True, timeout=300):
    result = subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(argv)} failed: {result.stdout} {result.stderr}")
    return result


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_DOCKER=1 with a reachable Docker daemon")
class RealDockerDeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker("info")
        cls.app = "fixture-" + secrets.token_hex(3)
        for target, pattern in ((release_state, "_IMAGE"), (registry, "_REPOSITORY")):
            patcher = patch.object(target, pattern, IMAGE_PATTERN if pattern == "_IMAGE" else REPOSITORY_PATTERN)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name).resolve()
        cls.start_registry()
        cls.images = cls.build_images()
        cls.addClassCleanup(cls.remove_app_resources)

    @classmethod
    def start_registry(cls):
        name = "shiplord-test-registry-" + secrets.token_hex(3)
        docker("run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{REGISTRY_PORT}:5000", "registry:2")
        cls.addClassCleanup(lambda: docker("rm", "-f", name, check=False))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{REGISTRY_PORT}/v2/", timeout=2)
                return
            except OSError:
                time.sleep(0.3)
        raise AssertionError("the local registry did not start")

    @classmethod
    def build_images(cls) -> dict[str, dict]:
        context = cls.base / "fixture"
        context.mkdir()
        (context / "server.py").write_text(SERVER)
        (context / "Dockerfile").write_text(DOCKERFILE)
        images = {}
        for version, mode, commit in (("v1", "healthy", "a"), ("v2", "healthy", "b"), ("v3", "unhealthy", "c"),
                                      ("v4", "crash", "d"), ("v5", "healthy", "e")):
            tag = f"{REPOSITORY}:{version}"
            docker("build", "-q", "--build-arg", f"VERSION={version}", "--build-arg", f"MODE={mode}", "--build-arg",
                   f"REVISION={commit * 40}", "-t", tag, str(context))
            pushed = docker("push", tag).stdout
            digest = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed).group(1)
            docker("rmi", tag, check=False)
            docker("rmi", f"{REPOSITORY}@{digest}", check=False)  # force every deploy to pull
            images[version] = {"image": f"{REPOSITORY}@{digest}", "commit": commit * 40}
        return images

    @classmethod
    def remove_app_resources(cls):
        ids = docker("ps", "-aq", "--filter", f"label=shiplord.app={cls.app}", check=False).stdout.split()
        if ids:
            docker("rm", "-f", *ids, check=False)
        docker("volume", "rm", "-f", f"shiplord-{cls.app}-artifacts", check=False)
        for image in cls.images.values():
            docker("rmi", image["image"], check=False)

    def setUp(self):
        self.layout = Layout(self.base / "host")
        for directory, mode in ((self.layout.var, 0o755), (self.layout.environments / self.app, 0o700),
                                (self.layout.app_state / self.app, 0o700), (self.layout.caddy_root / "ship", 0o755)):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(mode)
        self.layout.caddy_root.chmod(0o755)
        main = self.layout.caddy_root / "Caddyfile"
        main.write_text(f"import {self.layout.caddy_root}/ship/*.caddy\n")
        main.chmod(0o644)
        self.var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.state_fd = os.open(self.layout.app_state / self.app, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.var_fd)
        self.addCleanup(os.close, self.state_fd)
        self.lock = HostLock(self.var_fd)
        self.state = ReleaseState(self.app, self.state_fd, self.lock)
        with self.lock.acquire():
            if not (self.layout.app_state / self.app / "state.json").exists():
                self.state.initialize()
        self.port = getattr(self.__class__, "port", None) or next(p for p in PORT_RANGE if port_is_free(p))
        self.__class__.port = self.port
        self.registration = validate_registration({
            "schema_version": 1, "application_id": self.app, "deployment_uid": 1001, "deployment_user": "ship-fixture",
            "runtime": "docker", "api_domain": "fixture.example.test", "host_port": self.port,
            "image_repository": REPOSITORY, "max_memory_mb": 4096})
        self.runtime = DockerRuntime(HostSystem(), self.layout)
        self.deployer = Deployer(registration=self.registration, state=self.state,
                                 routes=RouteManager(self.layout.caddy_root, self.lock, FakeCaddy()),
                                 runtime=self.runtime, probes=Probes(admin_port=1))

    # helpers ---------------------------------------------------------------
    def configuration(self, **resources):
        return {"schema_version": 1, "name": self.app, "server": "production", "template": "google-adk",
                "runtime": "docker", "http": {"api_domain": "fixture.example.test",
                                              "frontend_origins": ["https://app.example.test"], "container_port": 8080,
                                              "readiness_path": "/ready"},
                "resources": {"memory_mb": 512}, "deploy": {"readiness_timeout_seconds": 6, "retain_successful_releases": 2}}

    def request(self, request_id, version, sequence, **overrides):
        image = self.images[version]
        value = {"request_id": request_id, "commit": image["commit"], "sequence": sequence,
                 "artifact": {"kind": "docker", "image": image["image"]}, "configuration": self.configuration()}
        value.update(overrides)
        return value

    def deploy(self, *args, **kwargs):
        with self.lock.acquire():
            return self.deployer.deploy(self.request(*args, **kwargs))

    def get(self, path):
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as response:
                    return response.read().decode()
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def containers(self):
        return docker("ps", "-aq", "--filter", f"label=shiplord.app={self.app}").stdout.split()

    def inspect(self):
        (container,) = self.containers()
        return json.loads(docker("inspect", container).stdout)[0]

    def assertOutcome(self, outcome, status):
        self.assertEqual(outcome.status, status, json.dumps(outcome.detail, indent=1)[:1500])

    # the lifecycle, in order: each step builds on the previous one -------------
    def test_lifecycle(self):
        first = self.deploy("req-1", "v1", 1000)
        self.assertOutcome(first, "succeeded")
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(first.detail["probes"]["local_ready"]["ready"], True)
        self.assertEqual(first.detail["image"]["revision_label"], "a" * 40)

        details = self.inspect()
        host = details["HostConfig"]
        self.assertEqual((host["Memory"], host["MemorySwap"]), (512 * 1024 * 1024, 512 * 1024 * 1024))
        self.assertEqual(host["RestartPolicy"]["Name"], "unless-stopped")
        self.assertEqual(host["PortBindings"]["8080/tcp"], [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}])
        self.assertEqual(host["LogConfig"]["Config"], {"max-file": "3", "max-size": "10m"})
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges", " ".join(host["SecurityOpt"] or []))
        self.assertEqual(details["Config"]["Labels"]["shiplord.app"], self.app)
        self.assertEqual([m["Name"] for m in details["Mounts"]], [f"shiplord-{self.app}-artifacts"])
        self.assertEqual(self.get("/whoami"), "1000")
        environment = json.loads(self.get("/env"))
        self.assertEqual(environment["ALLOW_ORIGINS"], '["https://app.example.test"]')
        self.assertEqual((environment["SERVE_WEB_INTERFACE"], environment["RELOAD_AGENTS"], environment["HOST"],
                          environment["AGENT_NAME"]), ("false", "false", "0.0.0.0", self.app))
        self.assertEqual(self.get("/counter"), "1")

        second = self.deploy("req-2", "v2", 1001)
        self.assertOutcome(second, "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "2")  # the artifacts volume survived the replacement
        self.assertEqual(len(self.containers()), 1)

        unhealthy = self.deploy("req-3", "v3", 1002)
        self.assertOutcome(unhealthy, "rolled_back")
        self.assertEqual(unhealthy.exit_code, 5)
        self.assertEqual(unhealthy.detail["kind"], "timeout")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "3")

        crashing = self.deploy("req-4", "v4", 1003)
        self.assertOutcome(crashing, "rolled_back")
        self.assertEqual(crashing.detail["kind"], "exited")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.state.read_state()["current"]["commit"], "b" * 40)
        self.assertIsNone(self.state.read_journal())

        for failed in ("v3", "v4"):  # a candidate that failed leaves no image behind
            self.assertNotEqual(docker("image", "inspect", self.images[failed]["image"], check=False).returncode, 0, failed)

        rolled_back = self.runtime_rollback("req-5")
        self.assertOutcome(rolled_back, "succeeded")
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(self.state.read_state()["previous"]["commit"], "b" * 40)

        # a worker killed mid-transaction: the journal says so, recovery restores the committed release
        with self.lock.acquire():
            self.state.begin("req-6", self.state.read_state()["previous"] | {"sequence": 2000})
            self.state.advance("runtime-changing")
            docker("rm", "-f", *self.containers())
        self.assertEqual(self.deploy("req-7", "v5", 2001).detail["error"], "recovery_required")
        with self.lock.acquire():
            recovered = self.deployer.recover()
        self.assertOutcome(recovered, "recovered")
        self.assertEqual(self.get("/version"), "v1")
        self.assertIsNone(self.state.read_journal())

        logs = self.runtime.logs(self.registration, 50)
        self.assertIn("fixture started version=v1", logs)

        # preparation failures leave the running container untouched
        before = self.containers()
        wrong_label = self.deploy("req-8", "v5", 2002, commit="f" * 40)
        self.assertOutcome(wrong_label, "failed")
        self.assertEqual(wrong_label.detail["error"], "prepare_failed")
        self.assertIn("different commit", wrong_label.detail["message"])
        missing = self.deploy("req-9", "v5", 2003, artifact={"kind": "docker", "image": f"{REPOSITORY}@sha256:" + "e" * 64})
        self.assertOutcome(missing, "failed")
        self.assertEqual(missing.detail["error"], "prepare_failed")
        self.assertEqual(self.containers(), before)
        self.assertEqual(self.get("/version"), "v1")

        # a custom environment file is passed through, but may never override managed values
        env_file = self.layout.environments / self.app / "app.env"
        env_file.write_text("CUSTOM_TOKEN=s3cret\n")
        env_file.chmod(0o600)
        with_env = self.deploy("req-10", "v5", 2004)
        self.assertOutcome(with_env, "succeeded")
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "s3cret")
        self.assertEqual(self.get("/version"), "v5")
        env_file.write_text("PORT=1\n")
        broken = self.deploy("req-11", "v1", 2005)  # v1 is still retained, so nothing is re-pulled that retention removed
        self.assertOutcome(broken, "rolled_back")
        self.assertIn("environment file is unusable", json.dumps(broken.detail))
        env_file.write_text("CUSTOM_TOKEN=s3cret\n")
        self.assertEqual(self.get("/version"), "v5")

        # retention: v5 deployed over v1 while v2 was previous, so v2's image left the window and was removed
        state = self.state.read_state()
        self.assertEqual((state["current"]["commit"], state["previous"]["commit"]), ("e" * 40, "a" * 40))
        self.assertNotEqual(docker("image", "inspect", self.images["v2"]["image"], check=False).returncode, 0)
        self.assertEqual(docker("image", "inspect", self.images["v1"]["image"], check=False).returncode, 0)
        self.assertEqual(docker("image", "inspect", self.images["v5"]["image"], check=False).returncode, 0)
        self.assertEqual(len(self.containers()), 1)

        status = self.deployer.status()
        self.assertEqual((status["running"], status["current"], status["unfinished_transaction"]),
                         (True, identity(state["current"]), None))
        self.assertEqual(release_id(state["current"]), self.inspect()["Config"]["Labels"]["shiplord.release"])

    def runtime_rollback(self, request_id):
        with self.lock.acquire():
            return self.deployer.rollback(request_id)


if __name__ == "__main__":
    unittest.main()
