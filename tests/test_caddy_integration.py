"""Route transactions against a real Caddy process (set SHIPLORD_TEST_CADDY=1)."""

from __future__ import annotations

import http.client
import http.server
import os
import ssl
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shiplord.registry import PORT_RANGE, port_is_free
from shiplord.release_state import HostLock
from shiplord.routing import RouteManager, RoutingError, SubprocessCaddy
from shiplord.system import run_command

ENABLED = os.environ.get("SHIPLORD_TEST_CADDY") == "1" and shutil.which("caddy")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def managed_port(skip: set[int]) -> int:
    return next(p for p in PORT_RANGE if p not in skip and port_is_free(p))


class AdminCaddy(SubprocessCaddy):
    """Real validate; reload through the admin API of the process this test started."""

    def __init__(self, caddyfile: Path, admin: str):
        super().__init__(caddy=shutil.which("caddy"))
        self.caddyfile, self.admin = caddyfile, admin

    def reload(self) -> None:
        self._run([self.caddy, "reload", "--config", str(self.caddyfile), "--adapter", "caddyfile",
                   "--address", self.admin], "reload")


def upstream(body: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_CADDY=1 with caddy installed")
class RealCaddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "caddy"
        (self.root / "ship").mkdir(parents=True)
        self.root.chmod(0o755)
        (self.root / "ship").chmod(0o755)
        self.https_port, self.http_port, self.admin_port = free_port(), free_port(), free_port()
        self.data_home = Path(self.temp.name).resolve() / "data"
        self.admin = f"127.0.0.1:{self.admin_port}"
        caddyfile = self.root / "Caddyfile"
        caddyfile.write_text(f"{{\n\tlocal_certs\n\tskip_install_trust\n\tauto_https disable_redirects\n"
                             f"\thttps_port {self.https_port}\n\thttp_port {self.http_port}\n\tadmin {self.admin}\n}}\n"
                             f"import {self.root}/ship/*.caddy\n")
        caddyfile.chmod(0o644)
        self.caddyfile = caddyfile
        self.process = subprocess.Popen([shutil.which("caddy"), "run", "--config", str(caddyfile), "--adapter",
                                         "caddyfile"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=dict(os.environ, XDG_DATA_HOME=str(self.data_home)))
        self.addCleanup(self.stop_caddy)
        self.wait_for(lambda: socket.create_connection(("127.0.0.1", self.admin_port), 0.2).close())
        lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(lock_dir.cleanup)
        fd = os.open(lock_dir.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.lock = HostLock(fd)
        self.manager = RouteManager(self.root, self.lock, AdminCaddy(caddyfile, self.admin))
        self.servers = []

    def stop_caddy(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for server in getattr(self, "servers", []):
            server.shutdown()
            server.server_close()

    @staticmethod
    def wait_for(check, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return check()
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def assert_route_gone(self):
        """With its only site removed, Caddy stops serving; any upstream body would mean the route lingers."""
        try:
            body = self.fetch("orders.example.com")
        except OSError:
            return
        raise AssertionError(f"route still served {body!r}")

    def start_upstream(self, body: bytes, skip=()) -> int:
        port = managed_port(set(skip))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), upstream(body))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return port

    def fetch(self, host: str) -> bytes:
        """HTTPS request to the local Caddy, verifying its certificate against Caddy's own local CA."""
        root = self.data_home / "caddy" / "pki" / "authorities" / "local" / "root.crt"
        context = ssl.create_default_context(cafile=str(root))  # raises until Caddy has issued its CA

        class Connection(http.client.HTTPSConnection):
            def connect(inner):
                sock = socket.create_connection(("127.0.0.1", self.https_port), timeout=5)
                inner.sock = context.wrap_socket(sock, server_hostname=host)

        connection = Connection(host, context=context, timeout=5)
        try:
            connection.request("GET", "/")
            return connection.getresponse().read()
        finally:
            connection.close()

    @staticmethod
    def registration(port, domain="orders.example.com"):
        return {"schema_version": 1, "application_id": "orders", "deployment_uid": 1001,
                "deployment_user": "ship-orders", "runtime": "docker", "api_domain": domain,
                "host_port": port, "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096}

    def test_apply_update_and_remove_change_real_traffic(self):
        first = self.start_upstream(b"first")
        second = self.start_upstream(b"second", skip={first})
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(self.registration(first)).changed)
            self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"first")
            self.assertTrue(self.manager.apply(self.registration(second)).changed)
            self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"second")
            self.assertFalse(self.manager.apply(self.registration(second)).changed)
            self.assertEqual(self.manager.reconcile("orders", self.registration(second)).changed, False)
            self.assertTrue(self.manager.remove("orders").changed)
            self.wait_for(self.assert_route_gone)

    def test_invalid_existing_route_is_reported_as_preexisting_and_nothing_changes(self):
        (self.root / "ship" / "broken.caddy").write_text("broken.example.com {\n\tnot_a_directive\n}\n")
        (self.root / "ship" / "broken.caddy").chmod(0o644)  # independent of the umask; group-writable files are refused
        before = {p.name: p.read_bytes() for p in (self.root / "ship").iterdir()}
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "already invalid"):
            self.manager.apply(self.registration(managed_port(set())))
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "ship").iterdir()}, before)

    def test_tls_style_or_scheme_duplicates_that_caddy_misses_are_still_refused(self):
        with open(self.caddyfile, "a") as file:
            file.write("http://orders.example.com:9999 {\n\trespond ok\n}\n")
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(self.registration(managed_port(set())))
        self.assertEqual(caught.exception.kind, "conflict")
        # Caddy itself accepts this configuration, so only the scan prevents the takeover.
        self.assertEqual(run_command([shutil.which("caddy"), "validate", "--config", str(self.caddyfile),
                                      "--adapter", "caddyfile"], env=dict(os.environ, HOME=str(self.data_home))).code, 0)

    def test_reload_failure_with_caddy_stopped_restores_the_file_and_flags_intervention(self):
        port = managed_port(set())
        self.process.terminate()
        self.process.wait(5)
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(self.registration(port))
        self.assertEqual(caught.exception.kind, "reload")
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(os.listdir(self.root / "ship"), [])

    def test_a_real_transaction_leaves_no_temporary_files(self):
        with self.lock.acquire():
            self.manager.apply(self.registration(managed_port(set())))
        self.assertEqual([p.name for p in self.root.rglob(".*")], [])


if __name__ == "__main__":
    unittest.main()
