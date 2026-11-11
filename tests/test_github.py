from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from shiplord import github, tailscale  # noqa: E402
from shiplord.client import ClientError  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.github import (SECRET_NAME, GhResult, GitHub, GitHubError, check_ci, ci_key_labels, fetch_host_keys,  # noqa: E402
                             generate_ci_key, github_setup, github_status, known_hosts_lines, label_for, parse_host_key,
                             resolve_ssh_host, rotate_key, upgrade_workflow)
from shiplord.keys import parse_public_key  # noqa: E402
from shiplord.manifest import validate_manifest  # noqa: E402
from shiplord.workflow import DEPLOY_WORKFLOW_PATH, PINS, WORKFLOW_PATH, render_deploy_workflow, render_workflow  # noqa: E402

REF = "cd" * 20
HOST = "server.example.com"
QUALITY_WORKFLOW = ".github/workflows/code-quality.yml"


def host_key(seed=1, key_type="ssh-ed25519"):
    def field(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value
    return key_type, base64.b64encode(field(key_type.encode()) + field(bytes([seed]) * 32)).decode()


def manifest(runtime="docker", with_github=False, server="production", template="google-adk", branch="main"):
    value = {"schema_version": 1, "name": "orders", "server": server, "template": template, "runtime": runtime,
             "http": {"api_domain": "orders.example.com", "frontend_origins": [], "container_port": 8080,
                      "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
             "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}
    if with_github:
        value["github"] = {"repository": "Example/orders", "deployment_branch": branch}
    return validate_manifest(value)


MANIFEST_TOML = '''schema_version = 1
name = "orders"
server = "production"
template = "google-adk"
runtime = "docker"
[http]
api_domain = "orders.example.com"
frontend_origins = []
container_port = 8080
readiness_path = "/ready"
[resources]
memory_mb = 512
[deploy]
readiness_timeout_seconds = 90
retain_successful_releases = 2
'''


class FakeGh:
    def __init__(self):
        self.calls, self.secrets, self.variables_, self.fail, self.permission = [], {}, {}, set(), "ADMIN"
        self.run = None
        self.oidc = {"use_default": True}
        self.dispatched = [{"databaseId": 7, "status": "completed", "url": "old"}]
        self.dispatch_error = None
        self.views = []

    def __call__(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        key = " ".join(argv[:2])
        if key in self.fail:
            return GhResult(1, "", f"{key} failed")
        if argv[:2] == ["api", "user"]:
            return GhResult(0, "octocat\n", "")
        if argv[0] == "api" and argv[1].endswith("/actions/oidc/customization/sub"):
            return GhResult(0, json.dumps(self.oidc), "")
        if argv[:2] == ["repo", "view"]:
            return GhResult(0, json.dumps({"viewerPermission": self.permission}), "")
        if argv[:2] == ["secret", "set"]:
            self.secrets[argv[2]] = stdin
            return GhResult(0, "", "")
        if argv[:2] == ["variable", "set"]:
            self.variables_[argv[2]] = stdin.decode()
            return GhResult(0, "", "")
        if argv[:2] == ["secret", "list"]:
            return GhResult(0, json.dumps([{"name": n} for n in self.secrets]), "")
        if argv[:2] == ["variable", "list"]:
            return GhResult(0, json.dumps([{"name": n, "value": v} for n, v in self.variables_.items()]), "")
        if argv[:2] == ["run", "list"] and "workflow_dispatch" in argv:
            return GhResult(0, json.dumps(self.dispatched), "")
        if argv[:2] == ["run", "list"]:
            return GhResult(0, json.dumps([self.run] if self.run else []), "")
        if argv[:2] == ["workflow", "run"]:
            if self.dispatch_error:
                return GhResult(1, "", self.dispatch_error)
            self.dispatched.insert(0, {"databaseId": 99, "status": "queued", "url": "https://github.com/run/99"})
            return GhResult(0, "", "")
        if argv[:2] == ["run", "view"]:
            return GhResult(0, json.dumps(self.views.pop(0) if len(self.views) > 1 else self.views[0]), "")
        raise AssertionError(argv)

    def order(self):
        return [" ".join(a[:2]) + (" " + a[2] if a[0] in ("secret", "variable") and a[1] == "set" else "")
                for a, _ in self.calls]


class FakeServer:
    """Registered CI keys, a fake admin channel, and an SSH runner that authenticates only registered keys."""

    def __init__(self):
        self.keys: dict[str, str] = {}
        self.admin_calls: list[tuple[str, dict]] = []
        self.fail_add = False
        self.fail_remove = set()
        self.ssh_events: list[str] = []

    def call(self, action, application_id, arguments):
        self.admin_calls.append((action, dict(arguments)))
        operation = arguments["operation"]
        if operation == "add":
            if self.fail_add:
                raise ClientError("the server refused the key", 3)
            self.keys[arguments["label"]] = parse_public_key(arguments["public_key"]).fingerprint
            return {"changed": True}
        if operation == "remove":
            if arguments["label"] in self.fail_remove:
                raise ClientError("could not remove", 1)
            self.keys.pop(arguments["label"], None)
            return {"changed": True}
        return {"keys": [{"label": k, "fingerprint": v} for k, v in self.keys.items()]}

    def runner(self, command, payload, timeout):
        identity = Path(command[command.index("-i") + 1])
        public = parse_public_key(Path(str(identity) + ".pub").read_text()).fingerprint
        known = next(part.split("=", 1)[1] for part in command if part.startswith("UserKnownHostsFile="))
        self.ssh_events.append(f"ssh:{public in self.keys.values()}:{Path(known).read_text().splitlines()[0].split()[0]}")
        request_id = read_frame(io.BytesIO(payload))["request_id"]
        message = {"status": "ok", "request_id": request_id, "version": 1, "helper_version": "0.1.0", "actions": ["deploy"]}
        if public not in self.keys.values():
            message = {"status": "error", "error": "access_denied"}
        out = io.BytesIO()
        write_frame(out, message)
        return out.getvalue(), b"", 0

    def admin(self):
        return self


class GitHubTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "ship.toml").write_text(MANIFEST_TOML)
        self.gh_runner, self.server = FakeGh(), FakeServer()
        self.gh = GitHub(self.gh_runner)

    def setup_call(self, **overrides):
        options = dict(manifest=manifest(), repository="Example/orders", project=self.project, admin=self.server, gh=self.gh,
                       admin_target="appuser@" + HOST, tooling_ref=REF, home=self.home, runner=self.server.runner,
                       host_keys=lambda target: [host_key(1), host_key(2, "ecdsa-sha2-nistp256")],
                       resolve=lambda host: (host, 22))
        options.update(overrides)
        manifest_value = options.pop("manifest")
        repository = options.pop("repository")
        return github_setup(manifest_value, repository, **options)

    def rotate_call(self, server=None, server_name="production", **overrides):
        server = server or self.server
        options = dict(admin=server, gh=self.gh, admin_target="appuser@" + HOST, home=self.home, runner=server.runner,
                       host_keys=lambda target: [host_key(1)], resolve=lambda host: (host, 22))
        options.update(overrides)
        return rotate_key(manifest(with_github=True, server=server_name), **options)

    def state_directory(self, server="production"):
        return self.home / "shiplord" / "ci" / server / "orders"

    def state(self, server="production"):
        return json.loads((self.state_directory(server) / "state.json").read_text())


class TailscaleCredentialTests(unittest.TestCase):
    """The automatable half of issue #48: create the trust credential when a token is present."""

    TOKEN = "tskey-api-throwaway-fixture-value"

    def setUp(self):
        self.gh = GitHub(FakeGh())

    def create(self, *, environ, oidc=None):
        if oidc is not None:
            self.gh.runner.oidc = oidc
        self.requests = []

        def transport(url, headers, body):
            self.requests.append((url, headers, body))
            return 200, json.dumps({"id": "id123456", "audience": "api.tailscale.com/id123456"}).encode()

        return github.tailscale_credential(self.gh, "Example/orders", manifest(), environ=environ,
                                           transport=transport)

    def test_without_a_token_the_printed_instructions_are_the_fallback(self):
        self.assertIsNone(self.create(environ={}))
        self.assertIsNone(self.create(environ={tailscale.API_TOKEN_ENV: "  "}))

    def test_a_token_creates_the_owner_wide_credential_so_later_repositories_reuse_it(self):
        settings, warnings = self.create(environ={tailscale.API_TOKEN_ENV: self.TOKEN},
                                         oidc={"use_default": True, "sub_claim_prefix": "repo:Example@11/orders@22"})
        self.assertEqual(settings, {"SHIPLORD_TS_CLIENT_ID": "id123456",
                                    "SHIPLORD_TS_AUDIENCE": "api.tailscale.com/id123456"})
        self.assertEqual(warnings, [])
        (url, headers, body), = self.requests
        payload = json.loads(body)
        self.assertEqual(payload["subject"], "repo:Example@11/*:ref:refs/heads/main")
        self.assertEqual((payload["issuer"], payload["scopes"], payload["tags"]),
                         (tailscale.GITHUB_ISSUER, ["auth_keys"], ["tag:ci"]))
        self.assertEqual(payload["description"], "shiplord: Example/orders")
        self.assertNotIn(self.TOKEN, json.dumps(settings) + json.dumps(warnings) + json.dumps(payload))

    def test_extra_tags_are_created_with_the_credential(self):
        requests = []

        def transport(url, headers, body):
            requests.append((url, headers, body))
            return 200, json.dumps({"id": "id123456", "audience": "api.tailscale.com/id123456"}).encode()

        github.tailscale_credential(self.gh, "Example/orders", manifest(), tags=["tag:ci", "tag:deploy"],
                                    environ={tailscale.API_TOKEN_ENV: self.TOKEN}, transport=transport)
        self.assertEqual(json.loads(requests[0][2])["tags"], ["tag:ci", "tag:deploy"])

    def test_a_refusal_does_not_echo_the_token(self):
        def refusing(url, headers, body):
            return 401, json.dumps({"message": f"bad token {self.TOKEN}"}).encode()
        with self.assertRaises(tailscale.TailscaleError) as caught:
            github.tailscale_credential(self.gh, "Example/orders", manifest(),
                                        environ={tailscale.API_TOKEN_ENV: self.TOKEN}, transport=refusing)
        self.assertNotIn(self.TOKEN, str(caught.exception))


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class SetupTests(GitHubTestCase):
    def test_generic_setup_reaches_the_key_secret_variables_and_workflow_steps(self):
        result = self.setup_call(manifest=manifest(template="generic"))
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.server.keys), 1)  # the CI key reached the server
        self.assertIn(SECRET_NAME, self.gh_runner.secrets)
        for name in ("SHIPLORD_SERVER_HOST", "SHIPLORD_DEPLOY_USER", "SHIPLORD_SSH_KNOWN_HOSTS"):
            self.assertIn(name, self.gh_runner.variables_)
        for path in (WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH):
            self.assertTrue((self.project / path).is_file(), path)
        self.assertIn('[github]\nrepository = "Example/orders"', (self.project / "ship.toml").read_text())
        self.assertEqual(set(self.state()["workflows"]), {WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH})

    def test_an_unsupported_request_is_refused_before_any_change_for_every_template(self):
        for template in ("google-adk", "generic"):
            with self.subTest(template=template):
                before = (self.project / "ship.toml").read_bytes()
                with patch.object(github, "generate_ci_key", side_effect=AssertionError("must not create a key")):
                    with self.assertRaises(GitHubError) as caught:
                        self.setup_call(manifest=manifest(template=template), repository="not a repo")
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(self.gh_runner.calls, [])
                self.assertEqual((self.server.admin_calls, self.server.ssh_events), ([], []))
                self.assertFalse(self.home.exists())
                self.assertEqual(list(self.project.iterdir()), [self.project / "ship.toml"])
                self.assertEqual((self.project / "ship.toml").read_bytes(), before)

    def test_setup_registers_proves_then_stores_the_key_sets_variables_and_writes_the_workflow(self):
        result = self.setup_call()
        order = self.gh_runner.order()
        self.assertLess(order.index("api user"), order.index("secret set SHIPLORD_SSH_PRIVATE_KEY"))
        self.assertEqual(self.server.admin_calls[0][0], "keys")
        self.assertEqual(self.server.ssh_events, [f"ssh:True:{HOST}"])  # proven against the pinned host key
        secret = self.gh_runner.secrets[SECRET_NAME]
        self.assertTrue(secret.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_SERVER_HOST"], HOST)
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_DEPLOY_USER"], "ship-orders")
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_SSH_KNOWN_HOSTS"],
                         known_hosts_lines(HOST, 22, [host_key(1), host_key(2, "ecdsa-sha2-nistp256")]))
        self.assertNotIn("SHIPLORD_SSH_PORT", self.gh_runner.variables_)
        workflow = (self.project / WORKFLOW_PATH).read_text()
        merged = manifest(with_github=True) | {"github": {"repository": "Example/orders", "deployment_branch": "main"}}
        self.assertEqual(workflow, render_workflow(merged, tooling_ref=REF))
        self.assertEqual((self.project / DEPLOY_WORKFLOW_PATH).read_text(),
                         render_deploy_workflow(merged, tooling_ref=REF))
        self.assertIn('[github]\nrepository = "Example/orders"', (self.project / "ship.toml").read_text())
        self.assertTrue(result["ok"] and result["github_account"] == "octocat")
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})

    def test_the_private_key_never_leaves_the_machine_except_through_the_secret_and_is_then_deleted(self):
        result = self.setup_call()
        ci_directory = self.state_directory()
        self.assertEqual([p.name for p in ci_directory.iterdir()], ["state.json"])
        self.assertEqual(oct(ci_directory.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((ci_directory / "state.json").stat().st_mode & 0o777), "0o600")
        key = self.gh_runner.secrets[SECRET_NAME].decode()
        blob = "".join(key.splitlines()[1:-1])
        everything = json.dumps(result) + json.dumps(self.state()) + json.dumps(self.server.admin_calls)
        everything += " ".join(" ".join(a) for a, _ in self.gh_runner.calls)
        self.assertNotIn(blob[:40], everything)
        for variable in self.gh_runner.variables_.values():
            self.assertNotIn(blob[:40], variable)
        self.assertIsNone(self.state()["private_key"])

    def test_the_key_is_proven_against_the_resolved_host_not_the_ssh_alias(self):
        # CI runs ssh with -F /dev/null, so an alias from ~/.ssh/config would never resolve there.
        targets = []

        def runner(command, payload, timeout):
            targets.append(command[-2])
            return self.server.runner(command, payload, timeout)
        self.setup_call(admin_target="appuser@my-alias", resolve=lambda host: ("100.64.0.9", 22), runner=runner)
        self.assertEqual(targets, ["ship-orders@100.64.0.9"])
        self.assertEqual(self.server.ssh_events, ["ssh:True:100.64.0.9"])
        self.assertEqual(self.gh_runner.variables_["SHIPLORD_SERVER_HOST"], "100.64.0.9")

    def test_repeating_setup_reuses_the_working_key(self):
        first = self.setup_call()
        before = dict(self.server.keys)
        second = self.setup_call()
        self.assertEqual(second["key_label"], first["key_label"])
        self.assertEqual(self.server.keys, before)
        self.assertEqual(len([c for c in self.server.admin_calls if c[1]["operation"] == "add"]), 1)
        self.assertEqual(len([a for a, _ in self.gh_runner.calls if a[:2] == ["secret", "set"]]), 1)
        self.assertEqual(second["steps"]["workflow"], f"unchanged ({DEPLOY_WORKFLOW_PATH}, {WORKFLOW_PATH})")

    def test_the_same_application_name_on_two_servers_keeps_separate_state(self):
        staging = FakeServer()
        first = self.setup_call()
        second = self.setup_call(manifest=manifest(server="staging"), admin=staging, runner=staging.runner)
        self.assertNotEqual(first["key_label"], second["key_label"])
        self.assertEqual(set(staging.keys), {second["key_label"]})
        self.assertTrue((self.state_directory() / "state.json").is_file())
        self.assertTrue((self.state_directory("staging") / "state.json").is_file())
        rotated = self.rotate_call()
        self.assertNotEqual(rotated["key_label"], first["key_label"])
        self.assertEqual(set(staging.keys), {second["key_label"]})  # the other server's key is untouched

    def test_state_from_the_old_location_is_migrated_and_the_key_reused(self):
        first = self.setup_call()
        legacy = self.home / "shiplord" / "ci" / "orders"
        os.replace(self.state_directory(), legacy)
        (self.home / "shiplord" / "ci" / "production").rmdir()
        second = self.setup_call()
        self.assertEqual(second["key_label"], first["key_label"])
        self.assertEqual(len([call for call in self.server.admin_calls if call[1]["operation"] == "add"]), 1)
        self.assertTrue((self.state_directory() / "state.json").is_file())
        self.assertFalse(legacy.exists())

    def test_an_interrupted_setup_is_resumed_after_the_old_state_is_migrated(self):
        self.server.fail_add = True
        with self.assertRaises(GitHubError):
            self.setup_call()
        fingerprint = self.state()["fingerprint"]
        self.assertTrue(Path(self.state()["private_key"]).is_file())
        legacy = self.home / "shiplord" / "ci" / "orders"
        os.replace(self.state_directory(), legacy)
        (self.home / "shiplord" / "ci" / "production").rmdir()
        self.server.fail_add = False
        result = self.setup_call()
        self.assertEqual(result["key_fingerprint"], fingerprint)  # the same key was resumed, not replaced
        self.assertEqual(set(self.server.keys), {result["key_label"]})
        self.assertIsNone(self.state()["private_key"])

    def test_missing_permission_or_authentication_stops_before_any_change(self):
        self.gh_runner.permission = "WRITE"
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual((caught.exception.code, caught.exception.step), (3, "access"))
        self.assertIn("ADMIN or MAINTAIN", str(caught.exception))
        self.assertEqual((self.server.admin_calls, self.gh_runner.secrets), ([], {}))
        self.gh_runner.fail.add("api user")
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual(caught.exception.code, 3)

    def test_a_github_section_already_in_the_file_is_never_duplicated(self):
        self.setup_call()
        text = (self.project / "ship.toml").read_text()
        self.assertEqual(text.count("[github]"), 1)
        self.setup_call(manifest=manifest())  # a caller holding a stale dict without the section
        self.assertEqual((self.project / "ship.toml").read_text(), text)
        (self.project / "ship.toml").write_text(text.replace("Example/orders", "Someone/else"))
        with self.assertRaisesRegex(GitHubError, "names Someone/else"):
            self.setup_call(manifest=manifest())

    def test_a_different_repository_than_the_manifest_names_is_refused(self):
        with self.assertRaisesRegex(GitHubError, "already names"):
            self.setup_call(manifest=manifest(with_github=True), repository="Other/repo")
        with self.assertRaisesRegex(GitHubError, "owner/repository"):
            self.setup_call(repository="not a repo")

    def test_a_workflow_generated_for_trunk_triggers_and_uses_the_subject_on_trunk(self):
        result = self.setup_call(deployment_branch="trunk")
        text = (self.project / WORKFLOW_PATH).read_text()
        self.assertIn('branches: ["trunk"]', text)
        self.assertNotIn("refs/heads/main", text)
        guidance = result["tailscale_setup"]
        self.assertEqual(guidance["subject_repository"], "repo:Example/orders:ref:refs/heads/trunk")
        self.assertEqual(guidance["subject_all_repositories"], "repo:Example/*:ref:refs/heads/trunk")
        self.assertIn("refs/heads/trunk", " ".join(guidance["steps"]))
        self.assertIn('deployment_branch = "trunk"', (self.project / "ship.toml").read_text())
        self.assertEqual(result["steps"]["manifest"], "ship.toml names Example/orders on trunk")
        expected = render_workflow(manifest() | {"github": {"repository": "Example/orders", "deployment_branch": "trunk"}},
                                   tooling_ref=REF)
        self.assertEqual(text, expected)

    def test_the_deployment_branch_defaults_to_main(self):
        result = self.setup_call()
        text = (self.project / WORKFLOW_PATH).read_text()
        self.assertIn('branches: ["main"]', text)
        self.assertEqual(result["tailscale_setup"]["subject_repository"], "repo:Example/orders:ref:refs/heads/main")
        self.assertIn('deployment_branch = "main"', (self.project / "ship.toml").read_text())

    def test_a_changed_branch_is_recorded_without_touching_the_rest_of_ship_toml(self):
        self.setup_call()
        text = (self.project / "ship.toml").read_text()
        text = text.replace('deployment_branch = "main"', 'deployment_branch = "main"  # current release branch')
        (self.project / "ship.toml").write_text(text)
        self.setup_call(deployment_branch="trunk")
        updated = (self.project / "ship.toml").read_text()
        self.assertEqual(updated.count("[github]"), 1)
        self.assertIn('deployment_branch = "trunk"  # current release branch', updated)
        self.assertEqual(updated.replace('deployment_branch = "trunk"', 'deployment_branch = "main"'), text)
        self.assertIn('branches: ["trunk"]', (self.project / WORKFLOW_PATH).read_text())

    def test_a_github_section_without_a_branch_gets_the_default_and_a_chosen_one(self):
        (self.project / "ship.toml").write_text(MANIFEST_TOML + '\n[github]\nrepository = "Example/orders"\n')
        self.setup_call(deployment_branch="trunk")
        self.assertIn('deployment_branch = "trunk"', (self.project / "ship.toml").read_text())
        (self.project / "ship.toml").write_text(MANIFEST_TOML + '\n[github]\nrepository = "Example/orders"\n')
        self.setup_call()
        self.assertIn('deployment_branch = "main"', (self.project / "ship.toml").read_text())

    def test_an_invalid_deployment_branch_is_refused_before_anything_changes(self):
        before = (self.project / "ship.toml").read_bytes()
        for branch in ("main..other", ".hidden", "-main", "release/", "main.lock", "main branch"):
            with self.subTest(branch=branch):
                with self.assertRaises(GitHubError) as caught:
                    self.setup_call(deployment_branch=branch)
                self.assertEqual((caught.exception.code, caught.exception.step), (2, "configuration"))
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual((self.server.admin_calls, self.server.ssh_events), ([], []))
        self.assertFalse(self.home.exists())
        self.assertEqual((self.project / "ship.toml").read_bytes(), before)

    def test_a_failure_registering_leaves_the_key_for_resumption_and_never_stores_the_secret(self):
        self.server.fail_add = True
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual(caught.exception.step, "register")
        self.assertEqual(self.gh_runner.secrets, {})
        retained = self.state()["private_key"]
        self.assertTrue(Path(retained).is_file())
        self.assertEqual(oct(Path(retained).stat().st_mode & 0o777), "0o600")
        fingerprint = self.state()["fingerprint"]
        self.server.fail_add = False
        result = self.setup_call()
        self.assertEqual(result["key_fingerprint"], fingerprint)  # the same key was resumed, not replaced
        self.assertFalse(Path(retained).exists())
        self.assertIn(SECRET_NAME, self.gh_runner.secrets)

    def test_a_ci_key_the_server_rejects_is_never_stored_in_github(self):
        original = self.server.call

        def register_nothing(action, application_id, arguments):
            if arguments["operation"] == "add":
                return {"changed": True}  # pretends to register but does not
            return original(action, application_id, arguments)
        self.server.call = register_nothing
        with self.assertRaises(GitHubError) as caught:
            self.setup_call()
        self.assertEqual(caught.exception.step, "verify")
        self.assertEqual(self.gh_runner.secrets, {})
        self.assertIsNotNone(self.state()["private_key"])

    def test_a_failure_storing_the_secret_is_resumable(self):
        self.gh_runner.fail.add("secret set")
        with self.assertRaises(GitHubError):
            self.setup_call()
        self.assertFalse(self.state()["secret_set"])
        self.assertTrue(Path(self.state()["private_key"]).is_file())
        self.gh_runner.fail.clear()
        self.assertTrue(self.setup_call()["ok"])
        self.assertTrue(self.state()["secret_set"])

    def test_an_edited_workflow_is_not_overwritten_without_force(self):
        self.setup_call()
        for path in (WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH):
            with self.subTest(path=path):
                target = self.project / path
                original = target.read_text()
                target.write_text(original + "# my change\n")
                with self.assertRaisesRegex(GitHubError, "was edited") as caught:
                    self.setup_call(manifest=manifest(with_github=True))
                self.assertIn(path, str(caught.exception))
                self.assertIn("# my change", target.read_text())
                self.setup_call(manifest=manifest(with_github=True), force_workflow=True)
                self.assertEqual(target.read_text(), original)

    def test_no_quality_job_is_generated_when_the_project_has_no_quality_workflow(self):
        self.setup_call()
        text = (self.project / WORKFLOW_PATH).read_text()
        self.assertNotIn("quality", text)
        self.assertNotIn("code-quality", text)

    def test_the_quality_job_uses_the_projects_reusable_workflow_when_it_exists(self):
        path = self.project / QUALITY_WORKFLOW
        path.parent.mkdir(parents=True)
        path.write_text("on: workflow_call\njobs: {}\n")
        self.setup_call()
        text = (self.project / WORKFLOW_PATH).read_text()
        self.assertIn(f"uses: ./{QUALITY_WORKFLOW}", text)
        merged = manifest(with_github=True) | {"github": {"repository": "Example/orders", "deployment_branch": "main"}}
        self.assertEqual(text, render_workflow(merged, tooling_ref=REF, quality_workflow=QUALITY_WORKFLOW))

    def test_the_quality_job_can_be_left_out_of_a_project_that_has_one(self):
        path = self.project / QUALITY_WORKFLOW
        path.parent.mkdir(parents=True)
        path.write_text("on: workflow_call\njobs: {}\n")
        self.setup_call(include_quality=False)
        self.assertNotIn("quality", (self.project / WORKFLOW_PATH).read_text())

    def test_an_explicit_quality_workflow_is_used_and_an_unusable_one_changes_nothing(self):
        self.setup_call(quality_workflow=".github/workflows/checks.yml")
        self.assertIn("uses: ./.github/workflows/checks.yml", (self.project / WORKFLOW_PATH).read_text())
        before = (self.project / WORKFLOW_PATH).read_text()
        for bad in ("code-quality.yml", WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH):
            with self.subTest(bad=bad), self.assertRaises(GitHubError) as caught:
                self.setup_call(quality_workflow=bad, force_workflow=True)
            self.assertEqual((caught.exception.code, caught.exception.step), (2, "workflow"))
            self.assertEqual((self.project / WORKFLOW_PATH).read_text(), before)

    def test_the_installed_version_is_the_default_pin_when_no_checkout_is_detected(self):
        from shiplord import __version__

        with patch("shiplord.github.detect_tooling_ref", return_value=None):
            result = self.setup_call(tooling_ref=None)
        self.assertIn(f"shiplord=={__version__}", (self.project / DEPLOY_WORKFLOW_PATH).read_text())
        self.assertIn("uses: QueryPlanner/shiplord/deploy@465a02fd0d76308546893d2cc37a94254e07c598",
                      (self.project / DEPLOY_WORKFLOW_PATH).read_text())
        self.assertNotIn("@None", (self.project / DEPLOY_WORKFLOW_PATH).read_text())
        self.assertIn(f"pinned at {__version__}", result["steps"]["workflow"])
        with patch("shiplord.github.detect_tooling_ref", return_value="ef" * 20):
            self.assertIn("efefefefefef", self.setup_call(tooling_ref=None)["steps"]["workflow"])

    def test_native_applications_get_credentials_and_a_workflow_that_builds_and_deploys(self):
        (self.project / "ship.toml").write_text(MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"'))
        command, path = "go build -trimpath -o dist/orders ./cmd/orders", "dist/orders"
        result = self.setup_call(manifest=manifest("native"), build_command=command, binary_path=path)
        self.assertIn(f"written ({WORKFLOW_PATH}", result["steps"]["workflow"])
        text = (self.project / WORKFLOW_PATH).read_text()
        self.assertIn(command, text)
        self.assertIn("Upload the binary", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("actions/download-artifact@", text)
        self.assertIn('shiplord deploy --binary "$BINARY"', text)
        expected = render_workflow(manifest("native") | {"github": {"repository": "Example/orders",
                                   "deployment_branch": "main", "build_command": command, "binary_path": path}},
                                   tooling_ref=REF)
        self.assertEqual(text, expected)
        manifest_text = (self.project / "ship.toml").read_text()
        self.assertIn(f'build_command = "{command}"', manifest_text)
        self.assertIn(f'binary_path = "{path}"', manifest_text)
        self.assertIn(SECRET_NAME, self.gh_runner.secrets)

    def test_native_setup_reuses_the_build_settings_recorded_in_the_manifest(self):
        (self.project / "ship.toml").write_text(MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"'))
        self.setup_call(manifest=manifest("native"), build_command="make", binary_path="build/orders")
        stored = manifest("native") | {"github": {"repository": "Example/orders", "deployment_branch": "main",
                                                  "build_command": "make", "binary_path": "build/orders"}}
        second = self.setup_call(manifest=stored)
        self.assertIn("unchanged", second["steps"]["workflow"])
        self.assertIn('build_command = "make"', (self.project / "ship.toml").read_text())

    def test_native_setup_without_build_settings_is_refused_before_any_change(self):
        (self.project / "ship.toml").write_text(MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"'))
        before = (self.project / "ship.toml").read_bytes()
        with patch.object(github, "generate_ci_key", side_effect=AssertionError("must not create a key")):
            with self.assertRaisesRegex(GitHubError, "--build-command") as caught:
                self.setup_call(manifest=manifest("native"))
        self.assertEqual((caught.exception.code, caught.exception.step), (2, "configuration"))
        self.assertEqual(self.gh_runner.calls, [])
        self.assertEqual((self.server.admin_calls, self.server.ssh_events), ([], []))
        self.assertFalse(self.home.exists())
        self.assertFalse((self.project / WORKFLOW_PATH).exists())
        self.assertEqual((self.project / "ship.toml").read_bytes(), before)

    def test_native_setup_refuses_an_existing_github_section_without_build_settings(self):
        native = MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"')
        (self.project / "ship.toml").write_text(native + '\n[github]\nrepository = "Example/orders"\ndeployment_branch = "main"\n')
        with self.assertRaisesRegex(GitHubError, "add both"):
            self.setup_call(manifest=manifest("native", with_github=True), build_command="make", binary_path="build/orders")
        self.assertEqual(self.gh_runner.calls, [])

    def test_native_build_flags_are_refused_for_a_docker_application(self):
        with self.assertRaisesRegex(GitHubError, "only to native"):
            self.setup_call(build_command="make")
        self.assertEqual(self.gh_runner.calls, [])

    def test_a_build_setting_that_differs_from_the_manifest_is_refused(self):
        stored = manifest("native") | {"github": {"repository": "Example/orders", "deployment_branch": "main",
                                                  "build_command": "make", "binary_path": "build/orders"}}
        with self.assertRaisesRegex(GitHubError, "already names github.build_command"):
            self.setup_call(manifest=stored, build_command="make release")
        self.assertEqual(self.gh_runner.calls, [])

    def test_unsafe_native_build_settings_are_refused_before_any_change(self):
        for build_command, binary_path in (("make\nrm -rf /", "build/orders"), ("make", "../orders"),
                                           ("make", "/usr/local/bin/orders"), ("make", "build//orders")):
            with self.subTest(build_command=build_command, binary_path=binary_path):
                with self.assertRaises(GitHubError) as caught:
                    self.setup_call(manifest=manifest("native"), build_command=build_command, binary_path=binary_path)
                self.assertEqual((caught.exception.code, caught.exception.step), (2, "configuration"))
        with self.assertRaisesRegex(GitHubError, "one binary"):
            self.setup_call(manifest=manifest("native"), build_command="make", binary_path="build/orders",
                            platforms="linux/amd64,linux/arm64")
        self.assertEqual(self.gh_runner.calls, [])

    def test_tailscale_settings_a_custom_ci_host_and_a_nondefault_port_are_carried_through(self):
        result = self.setup_call(ci_host="tail.example.ts.net", resolve=lambda host: (host, 2222),
                                 tailscale={"SHIPLORD_TS_CLIENT_ID": "id123", "SHIPLORD_TS_AUDIENCE": "aud"})
        variables = self.gh_runner.variables_
        self.assertEqual((variables["SHIPLORD_TS_CLIENT_ID"], variables["SHIPLORD_SSH_PORT"], variables["SHIPLORD_SERVER_HOST"]),
                         ("id123", "2222", "tail.example.ts.net"))
        self.assertTrue(variables["SHIPLORD_SSH_KNOWN_HOSTS"].startswith("[tail.example.ts.net]:2222 ssh-ed25519 "))
        self.assertEqual(result["server_host"], "tail.example.ts.net")
        self.assertNotIn("tailscale", result["steps"])

    def test_remaining_steps_do_not_ask_for_tailscale_settings_that_were_given(self):
        given = " ".join(self.setup_call(tailscale={"SHIPLORD_TS_CLIENT_ID": "id", "SHIPLORD_TS_AUDIENCE": "aud"})["remaining"])
        self.assertNotIn("--tailscale-client-id", given)
        self.assertIn("tag:ci", given)
        (self.project / "ship.toml").write_text(MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"'))
        native = " ".join(self.setup_call(
            manifest=manifest("native"), build_command="make", binary_path="build/orders")["remaining"])
        self.assertNotIn("registry login", native)

    def test_without_tailscale_settings_the_result_carries_the_exact_subject_for_immutable_subjects(self):
        self.gh_runner.oidc = {"use_default": True, "use_immutable_subject": True,
                               "sub_claim_prefix": "repo:Example@11/orders@22"}
        setup = self.setup_call()["tailscale_setup"]
        self.assertEqual(setup["subject_repository"], "repo:Example@11/orders@22:ref:refs/heads/main")
        self.assertEqual(setup["subject_all_repositories"], "repo:Example@11/*:ref:refs/heads/main")
        self.assertIn(setup["subject_all_repositories"], " ".join(setup["steps"]))

    def test_the_classic_subject_is_used_when_github_reports_no_prefix_or_cannot_be_read(self):
        self.assertEqual(self.setup_call()["tailscale_setup"]["subject_repository"],
                         "repo:Example/orders:ref:refs/heads/main")
        self.gh_runner.fail.add("api repos/Example/orders/actions/oidc/customization/sub")
        self.assertEqual(self.setup_call()["tailscale_setup"]["subject_all_repositories"],
                         "repo:Example/*:ref:refs/heads/main")

    def test_a_customized_subject_template_is_flagged(self):
        self.gh_runner.oidc = {"use_default": False, "include_claim_keys": ["repo", "context"]}
        self.assertIn("customized", self.setup_call()["tailscale_setup"]["warning"])

    def test_no_guidance_is_added_when_tailscale_is_configured(self):
        result = self.setup_call(tailscale={"SHIPLORD_TS_CLIENT_ID": "id123456", "SHIPLORD_TS_AUDIENCE": "aud"})
        self.assertNotIn("tailscale_setup", result)

    def test_the_remaining_manual_steps_are_stated_and_no_run_is_claimed(self):
        text = " ".join(self.setup_call()["remaining"])
        self.assertIn("Tailscale", text)
        self.assertIn("TAILSCALE_API_KEY", text)
        self.assertIn("No GitHub Actions run has been performed", text)


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class RotationTests(GitHubTestCase):
    def rotate(self, **overrides):
        return self.rotate_call(**overrides)

    def setUp(self):
        super().setUp()
        self.first = self.setup_call()

    def test_rotation_adds_proves_updates_github_and_only_then_revokes_the_old_key(self):
        old_secret = self.gh_runner.secrets[SECRET_NAME]
        result = self.rotate()
        self.assertNotEqual(result["key_label"], self.first["key_label"])
        self.assertEqual(result["old_label"], self.first["key_label"])
        self.assertEqual(result["old_labels"], [self.first["key_label"]])
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})
        operations = [(c[1]["operation"], c[1].get("label")) for c in self.server.admin_calls[1:]]
        self.assertEqual(operations, [("list", None), ("add", result["key_label"]), ("remove", self.first["key_label"])])
        self.assertNotEqual(self.gh_runner.secrets[SECRET_NAME], old_secret)
        self.assertEqual(self.server.ssh_events[-1], f"ssh:True:{HOST}")
        self.assertEqual(self.state()["label"], result["key_label"])
        self.assertEqual([p.name for p in self.state_directory().iterdir()], ["state.json"])

    def test_rotation_works_from_a_machine_that_never_ran_setup(self):
        shutil.rmtree(self.home)
        result = self.rotate()
        self.assertNotEqual(result["key_label"], self.first["key_label"])
        self.assertEqual(result["old_label"], self.first["key_label"])
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})
        self.assertEqual(self.state()["label"], result["key_label"])
        self.assertTrue(self.state()["secret_set"])

    def test_a_key_shiplord_cannot_identify_is_never_removed(self):
        foreign = "SHA256:" + base64.b64encode(bytes(range(32))).decode().rstrip("=")
        self.server.keys["personal"] = foreign
        result = self.rotate()
        self.assertEqual(self.server.keys["personal"], foreign)
        self.assertNotIn("personal", result["old_labels"])

    def test_two_leftover_ci_keys_are_both_revoked(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public = generate_ci_key(Path(directory), "orders", "leftover")
        leftover = label_for(parse_public_key(public).fingerprint)
        self.server.keys[leftover] = parse_public_key(public).fingerprint
        result = self.rotate()
        self.assertEqual(result["old_labels"], sorted([self.first["key_label"], leftover]))
        self.assertIsNone(result["old_label"])
        self.assertEqual(result["steps"]["revoke"], f"{', '.join(result['old_labels'])} removed")
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})

    def test_a_key_that_fails_verification_is_removed_and_the_old_key_stays_authoritative(self):
        old_secret = self.gh_runner.secrets[SECRET_NAME]
        original = self.server.call

        def register_nothing(action, application_id, arguments):
            if arguments["operation"] == "add":
                return {"changed": True}
            return original(action, application_id, arguments)
        self.server.call = register_nothing
        with self.assertRaises(GitHubError):
            self.rotate(admin=self.server)
        self.assertEqual(self.gh_runner.secrets[SECRET_NAME], old_secret)
        self.assertEqual(set(self.server.keys), {self.first["key_label"]})
        self.assertEqual(self.state()["label"], self.first["key_label"])

    def test_a_github_failure_leaves_the_old_key_working_and_unregisters_the_unused_new_one(self):
        old_secret = self.gh_runner.secrets[SECRET_NAME]
        self.gh_runner.fail.add("secret set")
        with self.assertRaises(GitHubError):
            self.rotate()
        self.assertEqual(self.gh_runner.secrets[SECRET_NAME], old_secret)
        self.assertEqual(set(self.server.keys), {self.first["key_label"]})
        self.assertEqual(self.state()["label"], self.first["key_label"])
        self.assertEqual([p.name for p in self.state_directory().iterdir()], ["state.json"])

    def test_a_failed_revocation_is_reported_loudly_with_the_new_key_already_live(self):
        self.server.fail_remove = {self.first["key_label"]}
        with self.assertRaises(GitHubError) as caught:
            self.rotate()
        self.assertEqual(caught.exception.step, "revoke")
        self.assertIn(self.first["key_label"], str(caught.exception))
        self.assertEqual(len(self.server.keys), 2)

    def test_a_failed_local_cache_write_never_leaves_two_ci_keys(self):
        with patch.object(github, "_save_state", side_effect=OSError("disk full")):
            result = self.rotate()
        self.assertEqual(self.server.keys, {result["key_label"]: result["key_fingerprint"]})

    def test_rotation_is_refused_before_any_change_when_the_server_has_no_ci_key(self):
        shutil.rmtree(self.home)
        self.server.keys.clear()
        secret = self.gh_runner.secrets[SECRET_NAME]
        with self.assertRaisesRegex(GitHubError, "run `shiplord github setup` first") as caught:
            self.rotate()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual([c[1]["operation"] for c in self.server.admin_calls[1:]], ["list"])
        self.assertEqual(self.gh_runner.secrets[SECRET_NAME], secret)
        self.assertFalse(self.home.exists())


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class StatusTests(GitHubTestCase):
    def status(self):
        return github_status(manifest(with_github=True), project=self.project, gh=self.gh, admin=self.server, home=self.home)

    def test_a_fully_configured_repository_reports_ok_except_the_missing_run(self):
        self.setup_call(tailscale={"SHIPLORD_TS_CLIENT_ID": "id", "SHIPLORD_TS_AUDIENCE": "aud"})
        code, result = self.status()
        failing = [name for name, check in result["checks"].items() if not check["ok"]]
        self.assertEqual((code, failing), (3, ["last_run"]))
        self.assertEqual(result["checks"]["last_run"]["note"], "no workflow run has happened yet")
        self.gh_runner.run = {"status": "completed", "conclusion": "success", "headSha": "a" * 40, "url": "u", "createdAt": "t"}
        code, result = self.status()
        self.assertEqual((code, result["ok"]), (0, True))

    def test_each_problem_is_named(self):
        self.setup_call()
        del self.gh_runner.secrets[SECRET_NAME]
        del self.gh_runner.variables_["SHIPLORD_DEPLOY_USER"]
        self.server.keys.clear()
        (self.project / WORKFLOW_PATH).unlink()
        self.gh_runner.permission = "READ"
        code, result = self.status()
        checks = result["checks"]
        self.assertEqual(code, 3)
        for name in ("access", "secret", "variables", "workflow", "server_key", "tailscale"):
            self.assertFalse(checks[name]["ok"], name)
        self.assertEqual(checks["variables"]["missing"], ["SHIPLORD_DEPLOY_USER"])

    def test_a_missing_deploy_workflow_is_reported(self):
        self.setup_call()
        (self.project / DEPLOY_WORKFLOW_PATH).unlink()
        code, result = self.status()
        self.assertEqual(code, 3)
        self.assertFalse(result["checks"]["workflow"]["ok"])
        self.assertEqual(result["checks"]["workflow"]["missing"], [DEPLOY_WORKFLOW_PATH])

    def test_a_manifest_without_github_is_a_usage_error(self):
        with self.assertRaises(GitHubError) as caught:
            github_status(manifest(), project=self.project, gh=self.gh, home=self.home)
        self.assertEqual(caught.exception.code, 2)


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class SetupCompleteTests(GitHubTestCase):
    """The local evidence `shiplord setup` uses to skip a finished github setup; it never queries GitHub."""

    def complete(self, repository="Example/orders", project=None):
        return github.setup_complete(manifest(with_github=True), project=project or self.project,
                                     repository=repository, home=self.home)

    def test_it_needs_the_manifest_workflow_and_a_stored_secret(self):
        self.assertFalse(self.complete())  # nothing has run yet
        self.setup_call()
        self.assertTrue(self.complete())

    def test_a_missing_workflow_or_a_different_repository_is_not_complete(self):
        self.setup_call()
        self.assertFalse(self.complete(repository="Other/repo"))
        self.assertFalse(self.complete(project=self.project / "elsewhere"))
        (self.project / WORKFLOW_PATH).unlink()
        self.assertFalse(self.complete())

    def test_a_secret_that_was_never_stored_is_not_complete(self):
        self.setup_call()
        state_path = github.ci_directory("orders", "production", self.home) / "state.json"
        state = json.loads(state_path.read_text())
        state["secret_set"] = False
        state_path.write_text(json.dumps(state))
        self.assertFalse(self.complete())

    def test_a_native_application_is_complete_only_with_its_generated_workflow(self):
        native = manifest(runtime="native", with_github=True)
        native["github"] = {**native["github"], "build_command": "make", "binary_path": "build/orders"}
        (self.project / "ship.toml").write_text(MANIFEST_TOML.replace('runtime = "docker"', 'runtime = "native"'))
        self.setup_call(manifest=native, build_command="make", binary_path="build/orders")
        self.assertTrue(github.setup_complete(native, project=self.project, repository="Example/orders", home=self.home))
        (self.project / WORKFLOW_PATH).unlink()
        self.assertFalse(github.setup_complete(native, project=self.project, repository="Example/orders", home=self.home))


class HostKeyTests(unittest.TestCase):
    def test_host_keys_are_validated_and_formatted_for_known_hosts(self):
        key_type, blob = host_key(3)
        self.assertEqual(parse_host_key(f"{key_type} {blob} root@server"), (key_type, blob))
        for bad in ("", "ssh-ed25519", "ssh-dss AAAA", f"ecdsa-sha2-nistp256 {blob}", "ssh-ed25519 !!!", "command=x ssh-ed25519 AAAA"):
            with self.subTest(bad=bad), self.assertRaises(GitHubError):
                parse_host_key(bad)
        self.assertEqual(known_hosts_lines("h.example.com", 22, [(key_type, blob)]), f"h.example.com {key_type} {blob}\n")
        self.assertEqual(known_hosts_lines("h.example.com", 2222, [(key_type, blob)]), f"[h.example.com]:2222 {key_type} {blob}\n")

    def test_the_host_keys_are_read_over_the_trusted_ssh_connection_not_a_keyscan(self):
        key_type, blob = host_key(4)
        seen = []

        def runner(command, payload, timeout):
            seen.append(command)
            return f"{key_type} {blob} root@s\n".encode(), b"", 0
        self.assertEqual(fetch_host_keys("appuser@server", runner), [(key_type, blob)])
        self.assertIn("StrictHostKeyChecking=yes", seen[0])
        self.assertIn("ssh_host_ed25519_key.pub", seen[0][-1])
        self.assertNotIn("keyscan", " ".join(seen[0]))
        with self.assertRaises(GitHubError):
            fetch_host_keys("appuser@server", lambda c, p, t: (b"", b"", 1))
        with self.assertRaises(GitHubError):
            fetch_host_keys("appuser@server", lambda c, p, t: (b"garbage line\n", b"", 0))

    @unittest.skipUnless(shutil.which("ssh"), "ssh is required")
    def test_an_alias_resolves_through_the_users_own_ssh_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text("Host prodalias\n  HostName real.example.com\n  Port 2200\n")
            self.assertEqual(resolve_ssh_host("prodalias", config), ("real.example.com", 2200))
            self.assertEqual(resolve_ssh_host("plain.example.com", config), ("plain.example.com", 22))
        with self.assertRaises(GitHubError):
            resolve_ssh_host("bad host; id")


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class KeyGenerationTests(unittest.TestCase):
    def test_a_private_ed25519_key_is_generated_and_labelled_from_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            private, public = generate_ci_key(Path(directory), "orders", "key-1")
            self.assertEqual(oct(private.stat().st_mode & 0o777), "0o600")
            key = parse_public_key(public)
            self.assertEqual(key.type, "ssh-ed25519")
            self.assertRegex(label_for(key.fingerprint), r"^ci-[a-z0-9]{8}$")
            other, _ = generate_ci_key(Path(directory), "orders", "key-2")
            self.assertNotEqual(private.read_bytes(), other.read_bytes())

    def test_only_a_label_derived_from_the_key_itself_marks_it_as_the_ci_key(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public = generate_ci_key(Path(directory), "orders", "key-1")
        key = parse_public_key(public)
        listed = [{"label": label_for(key.fingerprint), "fingerprint": key.fingerprint},
                  {"label": "personal", "fingerprint": key.fingerprint},
                  {"label": "ci-abcdef12", "fingerprint": "SHA256:zzz"},
                  {"label": None, "fingerprint": None}]
        self.assertEqual(ci_key_labels(listed), [label_for(key.fingerprint)])
        self.assertEqual(ci_key_labels([]), [])

    def test_the_gh_wrapper_sends_values_on_stdin_never_in_arguments(self):
        calls = []
        gh = GitHub(lambda argv, stdin=None: (calls.append((argv, stdin)), GhResult(0, "", ""))[1])
        gh.set_secret("o/r", "NAME", b"super-secret-value")
        gh.set_variable("o/r", "VAR", "multi\nline")
        for argv, stdin in calls:
            self.assertNotIn("super-secret-value", " ".join(argv))
            self.assertNotIn("multi", " ".join(argv))
        self.assertEqual([stdin for _, stdin in calls], [b"super-secret-value", b"multi\nline"])


STALE = "9" * 40


class UpgradeWorkflowTests(GitHubTestCase):
    """`github setup --upgrade-workflow`: only the pins change, and only in a file Shiplord wrote."""

    def pipeline(self, *, branch="main", quality=None, stale=("checkout",)):
        value = manifest(with_github=True) | {"github": {"repository": "Example/orders", "deployment_branch": branch}}
        texts = {DEPLOY_WORKFLOW_PATH: render_deploy_workflow(value, tooling_ref=REF),
                 WORKFLOW_PATH: render_workflow(value, tooling_ref=REF, quality_workflow=quality)}
        for name in stale:
            texts[DEPLOY_WORKFLOW_PATH] = texts[DEPLOY_WORKFLOW_PATH].replace(PINS[name].commit, STALE)
        for path, text in texts.items():
            target = self.project / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return texts

    def upgrade(self, **overrides):
        value = overrides.pop("manifest", manifest(with_github=True))
        return upgrade_workflow(value, project=self.project, home=self.home, **overrides)

    def read(self, path):
        return (self.project / path).read_text()

    def test_a_stale_pin_is_refreshed_and_every_other_line_is_preserved(self):
        texts = self.pipeline()
        (self.project / DEPLOY_WORKFLOW_PATH).write_text(texts[DEPLOY_WORKFLOW_PATH] + "# a local note\n")
        result = self.upgrade()
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], [DEPLOY_WORKFLOW_PATH])
        self.assertEqual(result["unchanged"], [WORKFLOW_PATH])
        self.assertEqual(result["pins"], [{"path": DEPLOY_WORKFLOW_PATH, "action": "actions/checkout",
                                           "from": STALE, "to": PINS["checkout"].commit, "version": "v4"}])
        self.assertEqual(self.read(DEPLOY_WORKFLOW_PATH),
                         texts[DEPLOY_WORKFLOW_PATH].replace(STALE, PINS["checkout"].commit) + "# a local note\n")
        self.assertEqual(self.read(WORKFLOW_PATH), texts[WORKFLOW_PATH])

    def test_a_hand_written_workflow_is_refused_before_anything_is_written(self):
        texts = self.pipeline()
        hand_written = ("name: Mine\non: push\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
                        f"      - uses: actions/checkout@{STALE}\n")
        (self.project / WORKFLOW_PATH).write_text(hand_written)  # the stale deploy workflow is accepted first
        with self.assertRaises(GitHubError) as caught:
            self.upgrade()
        self.assertEqual((caught.exception.code, caught.exception.step), (2, "workflow"))
        self.assertIn(WORKFLOW_PATH, str(caught.exception))
        self.assertEqual(self.read(WORKFLOW_PATH), hand_written)
        self.assertEqual(self.read(DEPLOY_WORKFLOW_PATH), texts[DEPLOY_WORKFLOW_PATH])  # not written either

    def test_the_deployment_branch_and_the_quality_job_survive(self):
        texts = self.pipeline(branch="release/prod", quality=QUALITY_WORKFLOW)
        self.assertEqual(self.upgrade()["changed"], [DEPLOY_WORKFLOW_PATH])
        self.assertEqual(self.read(WORKFLOW_PATH), texts[WORKFLOW_PATH])
        self.assertIn('branches: ["release/prod"]', self.read(WORKFLOW_PATH))
        self.assertIn(f"uses: ./{QUALITY_WORKFLOW}", self.read(WORKFLOW_PATH))
        self.assertIn("refs/heads/release/prod", self.read(DEPLOY_WORKFLOW_PATH))
        self.assertNotIn(STALE, self.read(DEPLOY_WORKFLOW_PATH))

    def test_a_missing_pipeline_file_is_refused(self):
        self.pipeline()
        (self.project / DEPLOY_WORKFLOW_PATH).unlink()
        with self.assertRaisesRegex(GitHubError, "github setup"):
            self.upgrade()
        self.assertFalse((self.project / DEPLOY_WORKFLOW_PATH).exists())

    def test_a_second_upgrade_has_nothing_left_to_do(self):
        self.pipeline()
        self.upgrade()
        result = self.upgrade()
        self.assertEqual((result["changed"], result["pins"]), ([], []))
        self.assertIn("already pin", result["note"])

    def test_a_native_application_refreshes_its_one_generated_file(self):
        native = manifest("native", with_github=True)
        native["github"] = {**native["github"], "build_command": "make", "binary_path": "build/orders"}
        text = render_workflow(native, tooling_ref=REF).replace(PINS["checkout"].commit, STALE)
        (self.project / WORKFLOW_PATH).parent.mkdir(parents=True, exist_ok=True)
        (self.project / WORKFLOW_PATH).write_text(text)
        result = self.upgrade(manifest=native)
        self.assertEqual(result["changed"], [WORKFLOW_PATH])
        self.assertNotIn(STALE, self.read(WORKFLOW_PATH))
        self.assertFalse((self.project / DEPLOY_WORKFLOW_PATH).exists())

    def record(self, digests):
        directory = self.state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text(json.dumps({"workflows": digests}))

    def test_the_recorded_digest_follows_a_file_shiplord_wrote(self):
        texts = self.pipeline()
        self.record({path: hashlib.sha256(text.encode()).hexdigest() for path, text in texts.items()})
        self.upgrade()
        recorded = self.state()["workflows"]
        for path in (DEPLOY_WORKFLOW_PATH, WORKFLOW_PATH):
            with self.subTest(path=path):
                self.assertEqual(recorded[path], hashlib.sha256((self.project / path).read_bytes()).hexdigest())

    def test_an_edited_file_is_not_recorded_as_shiplords_so_setup_still_refuses_it(self):
        texts = self.pipeline()
        clean = hashlib.sha256(texts[DEPLOY_WORKFLOW_PATH].encode()).hexdigest()
        self.record({DEPLOY_WORKFLOW_PATH: clean, WORKFLOW_PATH: hashlib.sha256(texts[WORKFLOW_PATH].encode()).hexdigest()})
        (self.project / DEPLOY_WORKFLOW_PATH).write_text(texts[DEPLOY_WORKFLOW_PATH] + "# a local note\n")
        self.upgrade()
        self.assertIn("# a local note", self.read(DEPLOY_WORKFLOW_PATH))
        self.assertEqual(self.state()["workflows"][DEPLOY_WORKFLOW_PATH], clean)  # still "the user edited it"


if __name__ == "__main__":
    unittest.main()


def check_view(conclusion="success", failed=None):
    names = ["Install the pinned Shiplord", "Join the tailnet", "Prepare the SSH identity and pinned host keys",
             "Check access only", "Deploy"]
    steps, seen = [], False
    for name in names:
        if name == "Deploy":
            state = "skipped"
        elif seen:
            state = "skipped"
        elif name == failed:
            state, seen = "failure", True
        else:
            state = "success"
        steps.append({"name": name, "conclusion": state})
    return {"status": "completed", "conclusion": conclusion, "url": "https://github.com/run/99",
            "jobs": [{"name": "quality", "steps": []}, {"name": "deploy", "steps": steps}]}


class CheckCiTests(unittest.TestCase):
    def setUp(self):
        self.gh_runner = FakeGh()
        self.gh = GitHub(self.gh_runner)
        self.slept = []
        self.now = [0.0]

    def check(self, timeout=1200, **changes):
        def sleep(seconds):
            self.slept.append(seconds)
            self.now[0] += seconds
        return check_ci(manifest(with_github=True, **changes), gh=self.gh, timeout=timeout,
                        clock=lambda: self.now[0], sleep=sleep)

    def test_a_check_dispatches_check_only_on_the_deployment_branch_and_waits_for_the_new_run(self):
        self.gh_runner.views = [{"status": "in_progress"}, check_view()]
        code, result = self.check()
        self.assertEqual((code, result["ok"], result["run"]), (0, True, "https://github.com/run/99"))
        dispatch = next(a for a, _ in self.gh_runner.calls if a[:2] == ["workflow", "run"])
        self.assertEqual(dispatch[dispatch.index("--ref") + 1], "main")
        self.assertIn("check_only=true", dispatch)
        self.assertEqual(result["steps"]["Deploy"], "skipped")
        self.assertEqual(self.slept, [10])

    def test_a_check_uses_the_configured_branch_for_the_dispatch_and_the_subject(self):
        self.gh_runner.oidc = {"use_default": True, "sub_claim_prefix": "repo:Example@1/orders@2"}
        self.gh_runner.views = [check_view("failure", "Join the tailnet")]
        code, result = self.check(branch="trunk")
        dispatch = next(a for a, _ in self.gh_runner.calls if a[:2] == ["workflow", "run"])
        self.assertEqual(dispatch[dispatch.index("--ref") + 1], "trunk")
        self.assertEqual((code, result["failed_step"]), (1, "Join the tailnet"))
        self.assertEqual(result["subject_expected"], "repo:Example@1/orders@2:ref:refs/heads/trunk")

    def test_a_tailscale_refusal_names_the_subject_github_presents(self):
        self.gh_runner.oidc = {"use_default": True, "sub_claim_prefix": "repo:Example@1/orders@2"}
        self.gh_runner.views = [check_view("failure", "Join the tailnet")]
        code, result = self.check()
        self.assertEqual((code, result["failed_step"]), (1, "Join the tailnet"))
        self.assertEqual(result["subject_expected"], "repo:Example@1/orders@2:ref:refs/heads/main")
        self.assertIn("subject", result["hint"])

    def test_an_ssh_refusal_points_at_the_ci_key(self):
        self.gh_runner.views = [check_view("failure", "Check access only")]
        code, result = self.check()
        self.assertEqual(code, 1)
        self.assertIn("github status --check-server", result["hint"])
        self.assertNotIn("subject_expected", result)

    def test_the_deploy_job_is_found_inside_the_called_workflow(self):
        view = check_view()
        for job in view["jobs"]:
            if job["name"] == "deploy":
                job["name"] = "deploy / deploy"  # a reusable workflow reports `caller / called`
        self.gh_runner.views = [view]
        code, result = self.check()
        self.assertEqual((code, result["steps"]["Check access only"]), (0, "success"))

    def test_a_workflow_without_the_check_mode_asks_for_regeneration(self):
        self.gh_runner.dispatch_error = "could not create workflow dispatch event: HTTP 422: Unexpected inputs provided"
        with self.assertRaisesRegex(GitHubError, "force-workflow"):
            self.check()

    def test_a_run_that_does_not_finish_is_reported_without_claiming_success(self):
        self.gh_runner.views = [{"status": "in_progress", "url": "u"}]
        code, result = self.check(timeout=30)
        self.assertEqual((code, result["ok"], result["status"]), (1, False, "in_progress"))

    def test_without_a_github_section_nothing_is_dispatched(self):
        with self.assertRaises(GitHubError):
            check_ci(manifest(), gh=self.gh)
        self.assertEqual(self.gh_runner.calls, [])
