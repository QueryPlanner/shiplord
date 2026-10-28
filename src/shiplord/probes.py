"""Readiness and routing probes.

Local readiness, proxy routing, public HTTPS, and DNS are reported separately
because they fail for different reasons (the app, the Caddy config, certificate
issuance, and the DNS provider respectively).
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
from typing import Callable

MAX_BODY_BYTES = 4 * 1024 * 1024
POLL_SECONDS = 1.0


class ProbeFailure(Exception):
    """A probe failed; ``kind`` is "exited", "timeout", or "routing"."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


class _WatchedConnection(http.client.HTTPConnection):
    """Remembers its socket: http.client moves it to the response (and clears ``sock``) once headers arrive."""

    watched: socket.socket | None = None

    def connect(self) -> None:
        super().connect()
        self.watched = self.sock

    def abort(self) -> None:
        if self.watched is not None:
            try:
                self.watched.shutdown(socket.SHUT_RDWR)  # wakes a blocked recv, which then ends the read
            except OSError:
                pass


def http_get(host: str, port: int, path: str, *, timeout: float = 2.0, headers: dict[str, str] | None = None,
             limit: int = MAX_BODY_BYTES, total: float | None = None, read_body: bool = True) -> tuple[int, bytes]:
    """GET without proxies or redirects; returns (status, body capped at ``limit``).

    ``timeout`` bounds each socket operation, which a peer that sends one byte at a time never trips, so ``total``
    (default: twice ``timeout`` plus a second) is a hard deadline for the whole exchange.
    """
    connection = _WatchedConnection(host, port, timeout=timeout)
    watchdog = threading.Timer(total if total is not None else timeout * 2 + 1, connection.abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        if not read_body:
            return response.status, b""
        body = response.read(limit)
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) <= limit and len(body) != int(declared):
            raise http.client.IncompleteRead(body, int(declared) - len(body))  # cut off: never pass off a partial body
        return response.status, body
    finally:
        watchdog.cancel()
        connection.close()


def _dials(node: object, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "dial" and isinstance(value, str):
                found.append(value)
            else:
                _dials(value, found)
    elif isinstance(node, list):
        for item in node:
            _dials(item, found)


def _routes_for_host(node: object, domain: str, found: list[dict]) -> None:
    if isinstance(node, dict):
        matches = node.get("match")
        if isinstance(matches, list) and any(isinstance(m, dict) and domain in (m.get("host") or []) for m in matches):
            found.append(node)
        for value in node.values():
            _routes_for_host(value, domain, found)
    elif isinstance(node, list):
        for item in node:
            _routes_for_host(item, domain, found)


def config_routes_to(config: object, domain: str, upstream: str) -> bool:
    """True if some route matching ``domain`` in a Caddy JSON config proxies to ``upstream``."""
    routes: list[dict] = []
    _routes_for_host(config, domain, routes)
    for route in routes:
        dials: list[str] = []
        _dials(route, dials)
        if upstream in dials:
            return True
    return False


class Probes:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 fetch=http_get, admin_port: int = 2019, https_port: int = 443,
                 https_context: ssl.SSLContext | None = None):
        self.clock, self.sleep, self.fetch = clock, sleep, fetch
        self.admin_port, self.https_port, self.https_context = admin_port, https_port, https_context

    def ready_once(self, registration: dict, release: dict) -> bool:
        path = release["configuration"]["http"]["readiness_path"]
        try:
            return self.fetch("127.0.0.1", registration["host_port"], path, read_body=False)[0] == 200
        except (OSError, http.client.HTTPException):
            return False

    def wait_ready(self, registration: dict, release: dict, alive: Callable[[], bool]) -> dict:
        """Poll the readiness path until 200, the process exits, or the deadline passes."""
        timeout = release["configuration"]["deploy"]["readiness_timeout_seconds"]
        deadline = self.clock() + timeout
        attempts = 0
        while True:
            attempts += 1
            if not alive():
                raise ProbeFailure("the application exited before it became ready", "exited")
            if self.ready_once(registration, release):
                return {"attempts": attempts, "ready": True}
            if self.clock() >= deadline:
                raise ProbeFailure(f"the application was not ready within {timeout} seconds", "timeout")
            self.sleep(POLL_SECONDS)

    def routing(self, registration: dict) -> bool | None:
        """Check the active Caddy configuration through its admin API; None if it cannot be read."""
        try:
            status, body = self.fetch("127.0.0.1", self.admin_port, "/config/apps/http/servers", timeout=3.0)
            if status != 200:
                return None
            config = json.loads(body.decode("utf-8"))
        except (OSError, http.client.HTTPException, ValueError):
            return None
        return config_routes_to(config, registration["api_domain"], f"127.0.0.1:{registration['host_port']}")

    def https(self, registration: dict, release: dict) -> dict:
        """Informational: does the public name serve over verified TLS (needs DNS and a certificate)?"""
        domain = registration["api_domain"]
        context = self.https_context or ssl.create_default_context()
        try:
            with socket.create_connection(("127.0.0.1", self.https_port), timeout=5) as raw:
                with context.wrap_socket(raw, server_hostname=domain) as tls:
                    path = release["configuration"]["http"]["readiness_path"]
                    tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode())
                    first = tls.recv(64).split(b"\r\n", 1)[0].decode("ascii", "replace")
            return {"verified": first.startswith("HTTP/1.1 200"), "status_line": first}
        except ssl.SSLCertVerificationError:
            return {"verified": False, "error": "certificate_not_trusted"}
        except (OSError, ssl.SSLError):
            return {"verified": False, "error": "connection_failed"}

    @staticmethod
    def dns(registration: dict) -> list[str]:
        try:
            infos = socket.getaddrinfo(registration["api_domain"], 443, type=socket.SOCK_STREAM)
        except OSError:
            return []
        return sorted({info[4][0] for info in infos})
