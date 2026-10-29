from __future__ import annotations

import http.server
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from shiplord.probes import ProbeFailure, Probes, config_routes_to, http_get

RELEASE = {"configuration": {"http": {"readiness_path": "/ready"}, "deploy": {"readiness_timeout_seconds": 5}}}
REGISTRATION = {"application_id": "orders", "api_domain": "orders.example.com", "host_port": 20007}


def serve(handler_class):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True).start()
    return server


def stop(server):
    server.shutdown()
    server.server_close()


def handler(responses):
    """Serve a scripted list of (status, body) responses, repeating the last one."""
    state = {"calls": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = responses[min(state["calls"], len(responses) - 1)]
            state["calls"] += 1
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    Handler.state = state
    return Handler


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ReadinessTests(unittest.TestCase):
    def probes(self, server, clock=None):
        clock = clock or FakeClock()
        port = server.server_address[1]
        return Probes(clock=clock, sleep=clock.sleep), dict(REGISTRATION, host_port=port), clock

    def test_becomes_ready_after_failures_using_real_http(self):
        server = serve(handler([(503, b""), (500, b""), (200, b"ok")]))
        self.addCleanup(stop, server)
        probes, registration, clock = self.probes(server)
        result = probes.wait_ready(registration, RELEASE, alive=lambda: True)
        self.assertEqual(result, {"attempts": 3, "ready": True})
        self.assertEqual(clock.sleeps, [1.0, 1.0])

    def test_only_status_200_counts_as_ready(self):
        server = serve(handler([(204, b""), (301, b""), (404, b"")]))
        self.addCleanup(stop, server)
        probes, registration, _ = self.probes(server)
        self.assertFalse(probes.ready_once(registration, RELEASE))

    def test_times_out_at_the_configured_deadline(self):
        server = serve(handler([(503, b"")]))
        self.addCleanup(stop, server)
        probes, registration, clock = self.probes(server)
        with self.assertRaises(ProbeFailure) as caught:
            probes.wait_ready(registration, RELEASE, alive=lambda: True)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertGreaterEqual(clock.now, 5)

    def test_an_exited_process_fails_immediately_without_polling(self):
        probes = Probes(clock=FakeClock(), sleep=lambda s: None, fetch=lambda *a, **k: self.fail("must not poll"))
        with self.assertRaises(ProbeFailure) as caught:
            probes.wait_ready(REGISTRATION, RELEASE, alive=lambda: False)
        self.assertEqual(caught.exception.kind, "exited")

    def test_a_closed_port_is_not_ready_and_does_not_raise(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(Probes().ready_once(dict(REGISTRATION, host_port=port), RELEASE))


def caddy_config(domain="orders.example.com", dial="127.0.0.1:20007"):
    return {"srv0": {"listen": [":443"], "routes": [{"match": [{"host": [domain]}], "handle": [{
        "handler": "subroute", "routes": [{"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": dial}]}]}]}],
        "terminal": True}]}}


class RoutingTests(unittest.TestCase):
    def test_config_matching_requires_both_host_and_upstream_in_the_same_route(self):
        self.assertTrue(config_routes_to(caddy_config(), "orders.example.com", "127.0.0.1:20007"))
        self.assertFalse(config_routes_to(caddy_config(dial="127.0.0.1:20009"), "orders.example.com", "127.0.0.1:20007"))
        self.assertFalse(config_routes_to(caddy_config(domain="other.example.com"), "orders.example.com", "127.0.0.1:20007"))
        two = {"a": caddy_config("a.example.com", "127.0.0.1:20007")["srv0"],
               "b": caddy_config("orders.example.com", "127.0.0.1:20009")["srv0"]}
        self.assertFalse(config_routes_to(two, "orders.example.com", "127.0.0.1:20007"))  # port belongs to another host
        self.assertFalse(config_routes_to({}, "orders.example.com", "127.0.0.1:20007"))
        self.assertFalse(config_routes_to("junk", "orders.example.com", "127.0.0.1:20007"))

    def admin(self, status, body):
        server = serve(handler([(status, body)]))
        self.addCleanup(stop, server)
        return Probes(admin_port=server.server_address[1])

    def test_reads_the_active_configuration_through_the_admin_api(self):
        self.assertTrue(self.admin(200, json.dumps(caddy_config()).encode()).routing(REGISTRATION))
        self.assertFalse(self.admin(200, json.dumps(caddy_config(dial="127.0.0.1:1")).encode()).routing(REGISTRATION))

    def test_an_unreadable_admin_api_is_unknown_not_false(self):
        self.assertIsNone(self.admin(500, b"").routing(REGISTRATION))
        self.assertIsNone(self.admin(200, b"not json").routing(REGISTRATION))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertIsNone(Probes(admin_port=port).routing(REGISTRATION))


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class HttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-days", "1", "-subj", "/CN=orders.example.com", "-addext", "subjectAltName=DNS:orders.example.com",
                        "-keyout", str(directory / "k.pem"), "-out", str(directory / "c.pem")],
                       check=True, capture_output=True)
        cls.cert, cls.key = directory / "c.pem", directory / "k.pem"

    def tls_server(self, response=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)

        def run():
            while True:
                try:
                    raw, _ = listener.accept()
                    with context.wrap_socket(raw, server_side=True) as tls:
                        tls.recv(1024)
                        tls.sendall(response)
                except (OSError, ssl.SSLError):
                    if listener.fileno() == -1:
                        return
        threading.Thread(target=run, daemon=True).start()
        return listener.getsockname()[1]

    def test_verifies_the_certificate_against_a_trusted_ca_and_reads_the_status(self):
        trusted = ssl.create_default_context(cafile=str(self.cert))
        probes = Probes(https_port=self.tls_server(), https_context=trusted)
        self.assertEqual(probes.https(REGISTRATION, RELEASE)["verified"], True)

    def test_an_untrusted_certificate_is_reported_not_ignored(self):
        probes = Probes(https_port=self.tls_server())
        self.assertEqual(probes.https(REGISTRATION, RELEASE), {"verified": False, "error": "certificate_not_trusted"})

    def test_a_non_200_response_and_a_closed_port_are_not_verified(self):
        trusted = ssl.create_default_context(cafile=str(self.cert))
        bad = Probes(https_port=self.tls_server(b"HTTP/1.1 502 Bad Gateway\r\n\r\n"), https_context=trusted)
        self.assertFalse(bad.https(REGISTRATION, RELEASE)["verified"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertEqual(Probes(https_port=port).https(REGISTRATION, RELEASE)["error"], "connection_failed")


class DnsTests(unittest.TestCase):
    def test_resolves_localhost_and_reports_failures_as_empty(self):
        self.assertIn("127.0.0.1", Probes.dns({"api_domain": "localhost"}) + ["127.0.0.1"])
        self.assertEqual(Probes.dns({"api_domain": "no-such-host.invalid"}), [])


class HttpGetTests(unittest.TestCase):
    def test_a_peer_that_drips_bytes_forever_is_cut_off_by_the_total_deadline(self):
        import time

        class Drip(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    for _ in range(15):  # ends by itself after three seconds
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.2)  # well inside the per-operation timeout, so only the total deadline can stop it
                except OSError:
                    pass

            def log_message(self, *args):
                pass
        server = serve(Drip)
        self.addCleanup(stop, server)
        started = time.monotonic()
        with self.assertRaises((OSError, http.client.HTTPException)):
            http_get("127.0.0.1", server.server_address[1], "/", timeout=2.0, total=0.7)
        self.assertLess(time.monotonic() - started, 3)

    def test_readiness_needs_only_the_status_line_and_reads_no_body(self):
        server = serve(handler([(200, b"x" * 100000)]))
        self.addCleanup(stop, server)
        status, body = http_get("127.0.0.1", server.server_address[1], "/", read_body=False)
        self.assertEqual((status, body), (200, b""))

    def test_body_is_capped_and_redirects_are_not_followed(self):
        server = serve(handler([(302, b"x" * 100)]))
        self.addCleanup(stop, server)
        status, body = http_get("127.0.0.1", server.server_address[1], "/", limit=10)
        self.assertEqual((status, len(body)), (302, 10))


if __name__ == "__main__":
    unittest.main()
