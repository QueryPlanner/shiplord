from __future__ import annotations

import io
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeProbes, configuration  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.client import AdminClient, ClientError, HelperClient  # noqa: E402
from shiplord.commands import CommandError  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.manifest import validate_manifest  # noqa: E402
from shiplord.runtime import RuntimeFailure  # noqa: E402
from shiplord.worker import run_job  # noqa: E402
from test_client import ClientTestCase, FakeSsh, frame, ok  # noqa: E402
from test_provision import ed25519  # noqa: E402

COMMIT, OTHER = "a" * 40, "c" * 40
IMAGE = "ghcr.io/example/orders@sha256:" + "b" * 64


def manifest(**changes):
    value = validate_manifest(configuration())
    value.update(changes)
    return value


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / ".ssh").mkdir()

    def test_public_key_comes_from_the_explicit_file_else_the_first_standard_key(self):
        (self.home / ".ssh" / "id_rsa.pub").write_text(ed25519(2) + "\n")
        (self.home / ".ssh" / "id_ed25519.pub").write_text(ed25519(1) + "\n")
        text, fingerprint = commands.read_public_key(None, self.home)
        self.assertEqual(text, ed25519(1))
        self.assertTrue(fingerprint.startswith("SHA256:"))
        explicit = self.home / "other.pub"
        explicit.write_text(ed25519(3))
        self.assertEqual(commands.read_public_key(explicit, self.home)[0], ed25519(3))

    def test_a_missing_or_unusable_key_is_a_clear_error(self):
        with self.assertRaises(CommandError) as caught:
            commands.read_public_key(None, self.home)
        self.assertEqual(caught.exception.code, 3)
        bad = self.home / "bad.pub"
        bad.write_text('command="x" ' + ed25519(1))
        with self.assertRaisesRegex(CommandError, "not a usable public key"):
            commands.read_public_key(bad, self.home)

    def test_the_image_repository_is_derived_from_github_and_lowercased(self):
        github = {"github": {"repository": "QueryPlanner/Orders", "deployment_branch": "main"}}
        self.assertEqual(commands.image_repository_for(manifest(**github), None), "ghcr.io/queryplanner/orders")
        self.assertEqual(commands.image_repository_for(manifest(), "ghcr.io/x/y"), "ghcr.io/x/y")
        with self.assertRaisesRegex(CommandError, "image repository"):
            commands.image_repository_for(manifest(), None)
        self.assertIsNone(commands.image_repository_for(manifest(runtime="native"), None))

    def test_the_release_sequence_is_the_commit_time_so_ci_and_local_agree(self):
        repo = self.home / "repo"
        repo.mkdir()
        environment = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                           GIT_COMMITTER_EMAIL="t@e", GIT_COMMITTER_DATE="2026-01-02T03:04:05+00:00",
                           GIT_AUTHOR_DATE="2026-01-02T03:04:05+00:00")
        run = lambda *args: subprocess.run(["git", *args], cwd=repo, env=environment, check=True, capture_output=True,  # noqa: E731
                                           text=True).stdout.strip()
        run("init", "-q")
        (repo / "f").write_text("x")
        run("add", "f")
        run("commit", "-q", "-m", "c", "--no-gpg-sign")
        sha = run("rev-parse", "HEAD")
        self.assertEqual(commands.commit_sequence(sha, None, repo), 1767323045)
        self.assertEqual(commands.commit_sequence(sha, 77, repo), 77)
        with self.assertRaises(CommandError):
            commands.commit_sequence(sha, -1, repo)
        with self.assertRaisesRegex(CommandError, "--sequence"):
            commands.commit_sequence("f" * 40, None, repo)
        with self.assertRaises(CommandError):
            commands.commit_sequence(sha, None, self.home)  # not a repository

    def test_deploy_inputs_are_validated_without_any_connection(self):
        commands.validate_deploy_inputs(manifest(), IMAGE, COMMIT)
        for image, commit in (("ghcr.io/example/orders:latest", COMMIT), (IMAGE, "HEAD"), (IMAGE, COMMIT[:39]),
                              ("ghcr.io/example/orders@sha256:abc", COMMIT), ("x; rm -rf /@sha256:" + "a" * 64, COMMIT)):
            with self.subTest(image=image, commit=commit), self.assertRaises(CommandError):
                commands.validate_deploy_inputs(manifest(), image, commit)
        with self.assertRaisesRegex(CommandError, "native"):
            commands.validate_deploy_inputs(manifest(runtime="native"), IMAGE, COMMIT)


class AdminCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / "k.pub"
        self.key.write_text(ed25519(1))

    def test_provision_sends_the_registration_inputs_and_explains_the_next_step(self):
        result = {"application_id": "orders", "deployment_user": "ship-orders", "host_port": 20000,
                  "api_domain": "orders.example.com", "runtime": "docker", "key_fingerprint": "SHA256:x", "created": {}}
        ssh = FakeSsh((ok(result), b"", 0))
        code, output = commands.provision_application(
            manifest(github={"repository": "Example/Orders", "deployment_branch": "main"}),
            AdminClient("appuser@server.example.com", runner=ssh), public_key=self.key)
        sent = read_frame(io.BytesIO(ssh.calls[0][1]))
        self.assertEqual(sent["action"], "provision")
        self.assertEqual(sent["arguments"]["image_repository"], "ghcr.io/example/orders")
        self.assertEqual(sent["arguments"]["public_key"], ed25519(1))
        self.assertRegex(sent["arguments"]["key_label"], r"^local-[a-z0-9]{8}$")
        self.assertEqual((code, output["deploy_login"]), (0, "ship-orders@server.example.com"))
        self.assertIn("DNS", output["next"])

    def test_empty_or_invalid_environment_input_is_refused_before_any_connection(self):
        ssh = FakeSsh()
        client = AdminClient("a@h", runner=ssh)
        for text, expected in (("", "no variables"), ("# only a comment\n\n", "no variables"), (None, "no variables"),
                               ("PORT=1\n", "managed by Shiplord"), ("BAD LINE\n", "not KEY=VALUE")):
            with self.subTest(text=text), self.assertRaisesRegex(CommandError, expected):
                commands.set_environment(manifest(), client, "set", text)
        self.assertEqual(ssh.calls, [])

    def test_environment_values_travel_in_the_frame_and_never_come_back(self):
        secret = "postgres://u:s3cr3t@db/x"
        ssh = FakeSsh((ok({"keys": ["DATABASE_URL"], "changed": True}), b"", 0))
        code, output = commands.set_environment(manifest(), AdminClient("a@h", runner=ssh), "set", f"DATABASE_URL={secret}\n")
        self.assertIn(secret.encode(), ssh.calls[0][1])
        self.assertNotIn("s3cr3t", str(ssh.calls[0][0]) + str(output))
        self.assertIn("next time a release starts", output["note"])
        _, listing = commands.set_environment(manifest(), AdminClient("a@h", runner=FakeSsh((ok({"keys": ["A"]}), b"", 0))), "keys")
        self.assertNotIn("note", listing)


class RegistryCommandTests(unittest.TestCase):
    TOKEN = "ghp_exampleTOKEN1234567890"

    def test_the_token_travels_only_in_the_frame_and_is_never_returned(self):
        ssh = FakeSsh((ok({"registries": [{"registry": "ghcr.io", "username": "bot"}]}), b"", 0))
        code, result = commands.registry_login(manifest(), AdminClient("a@h", runner=ssh), registry="ghcr.io",
                                               username="bot", token=self.TOKEN)
        command, payload = ssh.calls[0]
        self.assertIn(self.TOKEN.encode(), payload)
        self.assertNotIn(self.TOKEN, " ".join(command))
        self.assertNotIn(self.TOKEN, str(result))
        self.assertEqual((code, result["registries"]), (0, [{"registry": "ghcr.io", "username": "bot"}]))
        self.assertIn("read-only", result["note"])

    def test_invalid_input_is_refused_before_any_connection(self):
        ssh = FakeSsh()
        client = AdminClient("a@h", runner=ssh)
        for kwargs in ({"registry": "docker.io"}, {"username": "a b"}, {"token": "has space"}, {"token": ""},
                       {"token": "x" * 513}, {"token": "a\nb"}):
            options = dict(registry="ghcr.io", username="bot", token=self.TOKEN)
            options.update(kwargs)
            with self.subTest(kwargs=str(kwargs)[:30]), self.assertRaises(CommandError) as caught:
                commands.registry_login(manifest(), client, **options)
            self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertEqual(ssh.calls, [])

    def test_logout_and_status_send_no_secret_material(self):
        ssh = FakeSsh((ok({"registries": []}), b"", 0), (ok({"registries": []}), b"", 0))
        commands.registry_other(manifest(), AdminClient("a@h", runner=ssh), "logout")
        commands.registry_other(manifest(), AdminClient("a@h", runner=ssh), "list")
        sent = [read_frame(io.BytesIO(payload))["arguments"] for _, payload in ssh.calls]
        self.assertEqual(sent, [{"operation": "logout", "registry": "ghcr.io"}, {"operation": "list"}])


class DeployCommandTests(ClientTestCase):
    def deploy(self, **overrides):
        options = dict(image=IMAGE, commit=COMMIT, sequence=1000, request_id="req-1", timeout=60,
                       public_check=lambda domain, path: {"ok": True, "status": 200, "error": None})
        options.update(overrides)
        import shiplord.client as client_module
        original = client_module.wait_for_job
        run = lambda *a, **k: (run_job(self.services, "orders", options["request_id"]),  # noqa: E731
                               original(*a, **dict(k, interval=0, sleep=lambda s: None)))[1]
        client_module.wait_for_job = run
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        return commands.deploy_application(manifest(), self.client, **options)

    def test_a_successful_deploy_reports_the_release_and_the_public_check(self):
        code, result = self.deploy()
        self.assertEqual((code, result["ok"], result["status"]), (0, True, "succeeded"))
        self.assertEqual(result["release"]["commit"], COMMIT)
        self.assertEqual(result["public"], {"ok": True, "status": 200, "error": None})
        self.assertEqual((result["request_id"], result["url"]), ("req-1", "https://orders.example.com"))

    def test_public_verification_is_reported_but_only_fails_the_command_when_required(self):
        failing = lambda domain, path: {"ok": False, "status": None, "error": "dns_unresolved"}  # noqa: E731
        code, result = self.deploy(public_check=failing)
        self.assertEqual((code, result["ok"], result["public"]["error"]), (0, True, "dns_unresolved"))
        code, result = self.deploy(public_check=failing, request_id="req-2", require_public=True)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("public HTTPS check failed", result["error"])
        code, result = self.deploy(request_id="req-3", verify_public=False)
        self.assertNotIn("public", result)

    def test_failed_releases_return_the_documented_nonzero_exit_codes(self):
        self.deploy()
        from fakes import record
        from shiplord.runtime import release_id
        self.runtime.start_errors[release_id(record(commit="c", image="d", sequence=1001))] = RuntimeFailure("boom", "start")
        code, result = self.deploy(request_id="req-2", image="ghcr.io/example/orders@sha256:" + "d" * 64,
                                   commit=OTHER, sequence=1001)
        self.assertEqual((code, result["ok"], result["status"]), (5, False, "rolled_back"))
        self.assertNotIn("url", result)  # a failed release reports no live URL
        stale_code, stale = self.deploy(request_id="req-3", sequence=1)
        self.assertEqual((stale_code, stale["error"]), (1, "stale_release"))

    def test_invalid_input_is_rejected_before_any_remote_call(self):
        self.commands.clear()
        for kwargs in ({"commit": "main"}, {"image": "ghcr.io/example/orders:latest"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CommandError):
                self.deploy(**kwargs)
        self.assertEqual(self.commands, [])


class QueryCommandTests(ClientTestCase):
    def test_plan_status_and_logs_use_read_only_actions(self):
        code, plan = commands.plan_application(manifest(), self.client, image=IMAGE, commit=COMMIT, sequence=1000)
        self.assertEqual((code, plan["action"], plan["route_change"]), (0, "deploy", "create"))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.runtime.logs = lambda registration, tail: f"tail={tail}\n"
        self.assertEqual(commands.application_logs(self.client, 5)[1]["logs"], "tail=5\n")
        for tail in (0, 1001):
            with self.assertRaises(CommandError):
                commands.application_logs(self.client, tail)
        code, status = commands.application_status(self.client)
        self.assertEqual((code, status["application"]["current"], status["job"]), (0, None, None))

    def test_rollback_and_recover_run_as_jobs_and_return_their_exit_codes(self):
        import shiplord.client as client_module
        original = client_module.wait_for_job
        calls = []

        def wait(client, job_id, **options):
            run_job(self.services, "orders", job_id)
            calls.append(job_id)
            return original(client, job_id, **dict(options, interval=0, sleep=lambda s: None))
        client_module.wait_for_job = wait
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        code, result = commands.run_job_command(manifest(), self.client, "recover", request_id="rc-1")
        self.assertEqual((code, result["status"]), (0, "nothing_to_recover"))
        code, result = commands.run_job_command(manifest(), self.client, "rollback", request_id="rb-1")
        self.assertEqual((code, result["ok"], result["error"]), (1, False, "no_previous_release"))
        self.assertEqual(calls, ["rc-1", "rb-1"])


class DoctorTests(ClientTestCase):
    def doctor(self, **kwargs):
        options = dict(which=lambda name: "/usr/bin/" + name, resolve=lambda domain: [(0, 0, 0, "", ("192.0.2.1", 443))])
        options.update(kwargs)
        return commands.application_doctor(manifest(), self.client, **options)

    def test_all_checks_pass_and_are_reported_individually(self):
        code, result = self.doctor()
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual({name for name in result["checks"]}, {"ssh", "git", "gh", "server_access", "helper_actions",
                                                              "application", "dns"})
        self.assertEqual(result["checks"]["dns"]["addresses"], ["192.0.2.1"])

    def test_each_failure_is_named_and_returns_a_prerequisite_exit_code(self):
        code, result = self.doctor(which=lambda name: None if name == "gh" else "/usr/bin/" + name)
        self.assertEqual((code, result["checks"]["gh"]["ok"], result["checks"]["ssh"]["ok"]), (3, False, True))
        code, result = self.doctor(resolve=lambda domain: (_ for _ in ()).throw(socket.gaierror()))
        self.assertEqual((code, result["checks"]["dns"]["ok"]), (3, False))
        self.assertIn("DNS record", result["checks"]["dns"]["hint"])
        self.client.runner = lambda c, p, t: (frame({"status": "error", "error": "access_denied"}), b"", 3)
        code, result = self.doctor()
        self.assertEqual((code, result["checks"]["server_access"]["ok"]), (3, False))

    def test_an_unfinished_transaction_is_flagged_with_the_recovery_hint(self):
        self.state_begin()
        code, result = self.doctor()
        self.assertEqual(code, 3)
        self.assertEqual(result["checks"]["application"]["hint"], "run `shiplord recover`")

    def state_begin(self):
        from fakes import record
        from shiplord.release_state import HostLock, ReleaseState
        var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, var_fd)
        self.addCleanup(os.close, state_fd)
        lock = HostLock(var_fd)
        with lock.acquire():
            ReleaseState("orders", state_fd, lock).begin("req-x", record())


def tls_server(cert: Path, key: Path, statuses: list[int]) -> int:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    calls = iter(statuses + [statuses[-1]] * 50)

    def run():
        while True:
            try:
                raw, _ = listener.accept()
                with context.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(2048)
                    tls.sendall(f"HTTP/1.1 {next(calls)} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            except (OSError, ssl.SSLError):
                if listener.fileno() == -1:
                    return
    threading.Thread(target=run, daemon=True).start()
    tls_server.listener = listener
    return listener.getsockname()[1]


class PublicCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-days", "1", "-subj", "/CN=orders.example.com", "-addext", "subjectAltName=DNS:orders.example.com",
                        "-keyout", str(directory / "k.pem"), "-out", str(directory / "c.pem")], check=True, capture_output=True)
        cls.cert, cls.key = directory / "c.pem", directory / "k.pem"

    def check(self, statuses, trusted=True, **kwargs):
        port = tls_server(self.cert, self.key, statuses)
        self.addCleanup(tls_server.listener.close)
        context = ssl.create_default_context(cafile=str(self.cert)) if trusted else None
        options = dict(timeout=30, interval=0, sleep=lambda s: None, context=context, port=port, host="127.0.0.1")
        options.update(kwargs)
        return commands.check_public_https("orders.example.com", "/ready", **options)

    def test_a_verified_200_passes_and_retries_while_the_service_settles(self):
        self.assertEqual(self.check([200]), {"ok": True, "status": 200, "error": None})
        self.assertTrue(self.check([503, 502, 200])["ok"])

    def test_failures_are_classified_after_the_deadline(self):
        ticks = iter(range(0, 10000, 20))
        self.assertEqual(self.check([503], clock=lambda: next(ticks)), {"ok": False, "status": 503, "error": "unexpected_status"})
        ticks = iter(range(0, 10000, 20))
        self.assertEqual(self.check([200], trusted=False, clock=lambda: next(ticks))["error"], "certificate_not_trusted")
        ticks = iter(range(0, 10000, 20))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        result = commands.check_public_https("orders.example.com", "/ready", timeout=10, interval=0, sleep=lambda s: None,
                                             clock=lambda: next(ticks), port=closed, host="127.0.0.1")
        self.assertEqual(result["error"], "connection_failed")
        ticks = iter(range(0, 10000, 20))
        unresolved = commands.check_public_https("no-such-host.invalid", "/ready", timeout=10, interval=0,
                                                 sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(unresolved["error"], "dns_unresolved")


if __name__ == "__main__":
    unittest.main()
