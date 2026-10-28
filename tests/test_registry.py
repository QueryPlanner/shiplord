from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from shiplord.registry import PORT_RANGE, Registry, RegistryError, port_is_free, validate_registration
from shiplord.release_state import HostLock, StateError


def record(**changes):
    value = {"schema_version": 1, "application_id": "orders", "deployment_uid": 1001,
             "deployment_user": "ship-orders", "runtime": "docker", "api_domain": "orders.example.com",
             "host_port": 20000, "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096}
    value.update(changes)
    return value


def request(app="orders", **changes):
    value = {"deployment_uid": 1001, "deployment_user": "ship-orders", "runtime": "docker",
             "api_domain": f"{app}.example.com", "image_repository": f"ghcr.io/example/{app}"}
    value.update(changes)
    return value


class ValidationTests(unittest.TestCase):
    def test_valid_docker_and_native_records_round_trip(self):
        self.assertEqual(validate_registration(record()), record())
        native = record(runtime="native", image_repository=None)
        self.assertEqual(validate_registration(native, "orders"), native)

    def test_invalid_records_are_rejected(self):
        cases = {
            "extra field": record(extra=1),
            "missing field": {k: v for k, v in record().items() if k != "host_port"},
            "bool version": record(schema_version=True),
            "future version": record(schema_version=2),
            "bad app": record(application_id="Orders"),
            "bad domain": record(api_domain="Orders.Example.com"),
            "ip domain": record(api_domain="127.0.0.1"),
            "bool uid": record(deployment_uid=True),
            "root uid": record(deployment_uid=0),
            "bad user": record(deployment_user="ship orders"),
            "newline user": record(deployment_user="ship\nroot"),
            "bad runtime": record(runtime="vm"),
            "docker without repository": record(image_repository=None),
            "docker with tag": record(image_repository="ghcr.io/example/orders:latest"),
            "docker other registry": record(image_repository="docker.io/example/orders"),
            "native with repository": record(runtime="native"),
            "port below range": record(host_port=PORT_RANGE.start - 1),
            "port above range": record(host_port=PORT_RANGE.stop),
            "caddy admin port": record(host_port=2019),
            "bool port": record(host_port=True),
            "not an object": [],
        }
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaises(RegistryError):
                    validate_registration(value)

    def test_application_mismatch_is_rejected(self):
        with self.assertRaises(RegistryError):
            validate_registration(record(), "billing")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.lock_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.lock_temp.cleanup)
        lock_fd = os.open(self.lock_temp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, lock_fd)
        self.lock = HostLock(lock_fd)
        self.registry = Registry(self.fd, self.lock)
        self.busy: set[int] = set()

    def register(self, app="orders", **changes):
        return self.registry.register(app, probe=lambda port: port not in self.busy, **request(app, **changes))

    def test_registration_requires_the_host_lock(self):
        with self.assertRaisesRegex(RegistryError, "lock"):
            self.register()
        self.assertEqual(os.listdir(self.directory), [])

    def test_first_registration_gets_the_lowest_port_and_is_private(self):
        with self.lock.acquire():
            created = self.register()
        self.assertEqual(created["host_port"], PORT_RANGE.start)
        path = self.directory / "orders.json"
        self.assertEqual(json.loads(path.read_text()), created)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.registry.read("orders"), created)

    def test_registering_twice_is_idempotent_and_keeps_the_port(self):
        with self.lock.acquire():
            first = self.register()
            self.busy.add(first["host_port"])  # the app itself now listens there
            self.assertEqual(self.register(), first)
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_second_app_gets_a_distinct_port_skipping_listeners(self):
        with self.lock.acquire():
            first = self.register("orders")
            self.busy.add(first["host_port"] + 1)
            second = self.register("billing", deployment_uid=1002, deployment_user="ship-billing")
        self.assertEqual(second["host_port"], first["host_port"] + 2)
        self.assertEqual({r["application_id"] for r in self.registry.list()}, {"orders", "billing"})

    def test_changed_inputs_for_an_existing_app_are_refused(self):
        with self.lock.acquire():
            first = self.register()
            for change in ({"api_domain": "other.example.com"}, {"runtime": "native", "image_repository": None},
                           {"image_repository": "ghcr.io/example/other"}, {"deployment_uid": 2000},
                           {"deployment_user": "someone"}):
                with self.subTest(change=change):
                    with self.assertRaisesRegex(RegistryError, "differs"):
                        self.register(**change)
        self.assertEqual(self.registry.read("orders"), first)

    def test_hostname_and_account_conflicts_are_refused_without_writing(self):
        with self.lock.acquire():
            self.register("orders")
            with self.assertRaisesRegex(RegistryError, "hostname"):
                self.register("billing", api_domain="orders.example.com",
                              deployment_uid=1002, deployment_user="ship-billing")
            with self.assertRaisesRegex(RegistryError, "account"):
                self.register("billing")
            with self.assertRaisesRegex(RegistryError, "account"):
                self.register("billing", deployment_uid=1002)
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_invalid_input_is_rejected_before_any_write(self):
        with self.lock.acquire():
            for bad in ({"api_domain": "bad domain"}, {"runtime": "vm"}, {"deployment_uid": 0}):
                with self.subTest(bad=bad):
                    with self.assertRaises(RegistryError):
                        self.register(**bad)
            with self.assertRaises(ValueError):
                self.register("../escape")
        self.assertEqual(os.listdir(self.directory), [])

    def test_exhausted_port_range_fails_clearly(self):
        with self.lock.acquire():
            with self.assertRaisesRegex(RegistryError, "no loopback port"):
                self.registry.register("orders", probe=lambda port: False, **request())
        self.assertEqual(os.listdir(self.directory), [])

    def test_corrupt_unexpected_or_unsafe_entries_fail_closed(self):
        with self.lock.acquire():
            self.register()
            path = self.directory / "orders.json"
            for label, damage in {
                "corrupt json": lambda: path.write_text("{"),
                "wrong app inside": lambda: path.write_text(json.dumps(record(application_id="billing"))),
                "group writable": lambda: (path.write_text(json.dumps(record())), path.chmod(0o660)),
            }.items():
                with self.subTest(label):
                    damage()
                    with self.assertRaises(StateError):
                        self.registry.list()
                    with self.assertRaises(StateError):
                        self.register("billing", deployment_uid=1002, deployment_user="ship-billing")
        path.chmod(0o600)
        path.write_text(json.dumps(record()))
        (self.directory / "stray.txt").write_text("x")
        with self.assertRaisesRegex(RegistryError, "unexpected"):
            self.registry.list()

    def test_symlinked_registration_is_not_followed(self):
        os.symlink("/etc/hosts", self.directory / "orders.json")
        with self.assertRaises(StateError):
            self.registry.read("orders")

    def test_untrusted_directory_is_refused(self):
        self.directory.chmod(0o777)
        self.addCleanup(self.directory.chmod, 0o700)
        with self.assertRaises(StateError):
            Registry(self.fd, self.lock)


class PortProbeTests(unittest.TestCase):
    def test_probe_sees_a_real_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(port_is_free(port))
        self.assertTrue(port_is_free(port))


if __name__ == "__main__":
    unittest.main()
