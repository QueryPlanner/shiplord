from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from shiplord import __version__, cli, tailscale
from shiplord.client import AdminClient, ClientError, HelperClient
from shiplord.commands import CommandError, image_repository_for
from shiplord.configure import ConfigurationError, create_manifest
from shiplord.framing import read_frame, write_frame
from shiplord.manifest import ManifestError, load_manifest
from shiplord.transport import TransportError
from shiplord.workflow import DEPLOY_WORKFLOW_PATH, PINS, WORKFLOW_PATH, render_deploy_workflow, render_workflow

MANIFEST = '''schema_version = 1
name = "orders"
server = "production"
template = "google-adk"
runtime = "docker"
[http]
api_domain = "orders-api.example.com"
frontend_origins = ["https://orders.example.com"]
container_port = 8080
readiness_path = "/ready"
[resources]
memory_mb = 512
[deploy]
readiness_timeout_seconds = 90
retain_successful_releases = 2
[github]
repository = "QueryPlanner/orders"
deployment_branch = "main"
'''


def public_key_line() -> str:
    """A syntactically valid ed25519 public key line, without needing ssh-keygen."""
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


class ManifestTests(unittest.TestCase):
    def load(self, contents: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ship.toml"
            path.write_text(contents)
            return load_manifest(path)

    def test_valid_manifest_preserves_explicit_origins(self):
        result = self.load(MANIFEST)
        self.assertEqual(result["http"]["frontend_origins"], ["https://orders.example.com"])
        result = self.load(MANIFEST.replace('["https://orders.example.com"]', '[]'))
        self.assertEqual(result["http"]["frontend_origins"], [])

    def test_the_deployment_branch_defaults_to_main(self):
        result = self.load(MANIFEST.replace('\ndeployment_branch = "main"', ""))
        self.assertEqual(result["github"]["deployment_branch"], "main")

    def test_deployment_branch_names_are_validated(self):
        for branch in ("main", "trunk", "release/prod", "release/2026.01"):
            with self.subTest(branch=branch):
                result = self.load(MANIFEST.replace('deployment_branch = "main"', f'deployment_branch = "{branch}"'))
                self.assertEqual(result["github"]["deployment_branch"], branch)
        for branch in ("", " main", "main ", ".main", "-main", "main/", "main.", "main.lock", "a..b", "a//b", "a@{b", "a b"):
            with self.subTest(branch=branch), self.assertRaises(ManifestError) as caught:
                self.load(MANIFEST.replace('deployment_branch = "main"', f'deployment_branch = "{branch}"'))
            self.assertNotIn("secretvalue", str(caught.exception))

    def test_generic_is_an_explicit_docker_profile(self):
        contents = MANIFEST.replace('template = "google-adk"', 'template = "generic"')
        self.assertEqual(self.load(contents)["template"], "generic")
        with self.assertRaisesRegex(ManifestError, "generic template requires the docker runtime"):
            self.load(contents.replace('runtime = "docker"', 'runtime = "native"'))

    def test_an_image_table_names_a_supported_registry(self):
        without_github = MANIFEST.split("[github]")[0]
        hub = self.load(without_github + '[image]\nrepository = "docker.io/example/orders"\n')
        self.assertEqual(hub["image"], {"repository": "docker.io/example/orders"})
        self.assertEqual(image_repository_for(hub, None), "docker.io/example/orders")
        bare = self.load(without_github + '[image]\nrepository = "nginx"\n')
        self.assertEqual(bare["image"], {"repository": "docker.io/library/nginx"})
        self.assertEqual(image_repository_for(bare, None), "docker.io/library/nginx")
        # A [github] table and an [image] table may agree, but never disagree.
        agreeing = self.load(MANIFEST + '[image]\nrepository = "ghcr.io/queryplanner/orders"\n')
        self.assertEqual(agreeing["image"]["repository"], "ghcr.io/queryplanner/orders")
        with self.assertRaisesRegex(ManifestError, "two image repositories"):
            self.load(MANIFEST + '[image]\nrepository = "docker.io/example/orders"\n')
        for repository in ("docker.io/nginx", "example/orders", "quay.io/example/orders",
                           "docker.io/example/orders:latest", "docker.io/Example/orders", ""):
            with self.subTest(repository=repository), self.assertRaises(ManifestError):
                self.load(without_github + f'[image]\nrepository = "{repository}"\n')
        native = without_github.replace('runtime = "docker"', 'runtime = "native"')
        with self.assertRaisesRegex(ManifestError, "Docker applications"):
            self.load(native + '[image]\nrepository = "docker.io/example/orders"\n')

    def test_invalid_fields_are_rejected_without_echoing_values(self):
        replacements = [
            ("schema_version = 1", "schema_version = 2"),
            ("name = \"orders\"", "name = \"-bad\""),
            ('name = "orders"', 'name = ""'),
            ('server = "production"', 'server = "-invalid"'),
            ('template = "google-adk"', 'template = "unknown"'),
            ('runtime = "docker"', 'runtime = "unknown"'),
            ('[resources]\nmemory_mb = 512\n', ''),
            ("container_port = 8080", "container_port = true"),
            ("container_port = 8080", "container_port = 65536"),
            ("memory_mb = 512", "memory_mb = 0"),
            ("retain_successful_releases = 2", "retain_successful_releases = 1"),
            ("retain_successful_releases = 2", "retain_successful_releases = 3"),
            ('api_domain = "orders-api.example.com"', 'api_domain = "*.example.com"'),
            ('api_domain = "orders-api.example.com"', 'api_domain = "127.0.0.1"'),
            ('api_domain = "orders-api.example.com"', 'api_domain = "Example.com"'),
            ('readiness_path = "/ready"', 'readiness_path = "/ready?token=secretvalue"'),
            ('repository = "QueryPlanner/orders"', 'repository = "--bad/orders"'),
            ('deployment_branch = "main"', 'deployment_branch = "main..other"'),
            ('runtime = "docker"', 'runtime = "docker"\nunknown = "secretvalue"'),
            ('frontend_origins = ["https://orders.example.com"]', 'frontend_origins = "secretvalue"'),
            ('frontend_origins = ["https://orders.example.com"]', 'frontend_origins = ["https://example.com", "https://example.com"]'),
        ]
        for old, new in replacements:
            with self.subTest(value=new):
                with self.assertRaises(ManifestError) as caught:
                    self.load(MANIFEST.replace(old, new))
                self.assertNotIn("secretvalue", str(caught.exception))

    def test_invalid_origins_are_rejected(self):
        for origin in ["*", "https://*.example.com", "http://localhost", "https://user:secret@example.com", "https://example.com/path", "https://example.com/", "https://example.com?x=1", "https://example.com:99999", "https://example.com:", "https://example.com:0", "https://EXAMPLE.com", "https://127.0.0.1"]:
            with self.subTest(origin=origin):
                contents = MANIFEST.replace("https://orders.example.com", origin)
                with self.assertRaises(ManifestError):
                    self.load(contents)

    def test_malformed_toml_does_not_echo_content(self):
        with self.assertRaises(ManifestError) as caught:
            self.load('name = "secretvalue')
        self.assertNotIn("secretvalue", str(caught.exception))

    def test_native_github_build_settings_are_optional_but_validated(self):
        native = MANIFEST.replace('runtime = "docker"', 'runtime = "native"')
        self.assertNotIn("build_command", self.load(native)["github"])  # every other command still works
        settings = 'deployment_branch = "main"\nbuild_command = "go build -o dist/orders ./cmd/orders"\nbinary_path = "dist/orders"'
        loaded = self.load(native.replace('deployment_branch = "main"', settings))
        self.assertEqual((loaded["github"]["build_command"], loaded["github"]["binary_path"]),
                         ("go build -o dist/orders ./cmd/orders", "dist/orders"))
        with self.assertRaisesRegex(ManifestError, "only to native"):
            self.load(MANIFEST.replace('deployment_branch = "main"', settings))

    def test_native_build_settings_reject_unsafe_values(self):
        native = MANIFEST.replace('runtime = "docker"', 'runtime = "native"')
        for command, path in (("make\nrm -rf /", "build/orders"), ("", "build/orders"), ("make", ""),
                              ("make", "../orders"), ("make", "/usr/local/bin/orders"), ("make", "build//orders"),
                              ("make", "build/./orders"), ("make", "build/orders/")):
            settings = (f'deployment_branch = "main"\nbuild_command = {json.dumps(command)}\n'
                        f'binary_path = {json.dumps(path)}')
            with self.subTest(command=command, path=path), self.assertRaises(ManifestError):
                self.load(native.replace('deployment_branch = "main"', settings))

    def with_http(self, extra: str) -> dict:
        return self.load(MANIFEST.replace('api_domain = "orders-api.example.com"',
                                          'api_domain = "orders-api.example.com"\n' + extra))

    def test_additional_hostnames_and_the_www_alias_are_expanded(self):
        aliases = self.with_http('hostnames = ["legacy.example.com", "www.orders-api.example.com"]')
        self.assertEqual(aliases["http"]["hostnames"],
                         ["legacy.example.com", "www.orders-api.example.com"])
        self.assertEqual(self.with_http("www_redirect = true")["http"]["hostnames"],
                         ["www.orders-api.example.com"])
        # Without aliases the key is absent, so a record written before aliases existed validates unchanged.
        self.assertNotIn("hostnames", self.load(MANIFEST)["http"])

    def test_unusable_additional_hostnames_are_rejected(self):
        cases = {
            "duplicate": 'hostnames = ["legacy.example.com", "legacy.example.com"]',
            "the canonical hostname": 'hostnames = ["orders-api.example.com"]',
            "www outside the canonical hostname": 'hostnames = ["www.other.example.com"]',
            "uppercase": 'hostnames = ["Legacy.example.com"]',
            "ip address": 'hostnames = ["127.0.0.1"]',
            "wildcard": 'hostnames = ["*.example.com"]',
            "not a list": 'hostnames = "legacy.example.com"',
            "not strings": "hostnames = [1]",
            "www twice": 'hostnames = ["www.orders-api.example.com"]\nwww_redirect = true',
            "www_redirect not a boolean": 'www_redirect = "yes"',
        }
        for label, extra in cases.items():
            with self.subTest(label), self.assertRaises(ManifestError) as caught:
                self.with_http(extra)
            self.assertNotIn("other.example.com", str(caught.exception))

    def test_configuration_reports_unusable_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigurationError, "could not create ship.toml"):
                create_manifest(Path(directory) / "missing", "orders", "production", "api.example.com", [])


class CLITests(unittest.TestCase):
    def invoke(self, directory: str, *args: str, env=None):
        # The installed console script is the public boundary.
        executable = Path(os.environ.get("VIRTUAL_ENV", "")) / "bin" / "shiplord"
        if not executable.is_file():
            executable = Path(__import__("sys").executable).parent / "shiplord"
        if env is None:  # configure consults the registered servers; never read the developer's own configuration
            env = dict(os.environ, XDG_CONFIG_HOME=str(Path(directory) / ".test-config"))
        # A real Tailscale token in the developer's environment must never reach a test: it would call the API.
        env = {name: value for name, value in env.items() if name != tailscale.API_TOKEN_ENV}
        # stdin is not a terminal, so no command may wait for an answer.
        return subprocess.run([str(executable), *args], cwd=directory, text=True, capture_output=True, check=False,
                              env=env, stdin=subprocess.DEVNULL)

    def git(self, directory: str, *args: str) -> None:
        result = subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plan_uses_local_manifest_and_reports_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            result = self.invoke(directory, "plan", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["configuration"]["name"], "orders")
        self.assertIn("unknown", output["remote_changes"])

    def test_invalid_manifest_has_json_error_and_exit_two(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "plan", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_commands_that_need_a_manifest_fail_cleanly_without_one_and_write_nothing(self):
        commands = [["provision", "--json"], ["status", "--json"], ["logs", "--tail", "10"], ["rollback", "--json"],
                    ["restart", "--json"], ["recover", "--json"], ["plan", "--json"], ["env", "keys", "--json"],
                    ["release", "--json"],
                    ["deploy", "--image", "ghcr.io/example/orders@sha256:" + "a" * 64, "--json"]]
        with tempfile.TemporaryDirectory() as directory:
            for command in commands:
                with self.subTest(command=command):
                    result = self.invoke(directory, *command)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    if "--json" in command:
                        self.assertIn("ship.toml", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_setup_without_a_manifest_asks_for_a_repository_before_writing_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "setup", "--json")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--repo", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_deprovision_requires_explicit_confirmation_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            result = self.invoke(directory, "deprovision", "--retire", "--json", env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--yes", json.loads(result.stdout)["error"])

    def test_restore_requires_confirmation_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)  # no server aliases exist here
            result = self.invoke(directory, "restore", "--latest", "--json", env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--yes", json.loads(result.stdout)["error"])
            # With --yes the confirmation passes and the missing server alias is the next failure (exit 3).
            self.assertEqual(self.invoke(directory, "restore", "--latest", "--yes", "--json", env=env).returncode, 3)
            self.assertEqual(self.invoke(directory, "backup", "--json", env=env).returncode, 3)

    def test_backup_and_restore_need_a_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in (["backup", "--json"], ["restore", "--latest", "--yes", "--json"]):
                with self.subTest(command=command):
                    result = self.invoke(directory, *command)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("ship.toml", json.loads(result.stdout)["error"])

    def test_backup_output_reports_the_verified_archive(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli.output.render_operation(
                0, {"status": "succeeded", "archive": "shiplord-backup-orders-20240101T000000Z.tar.gz",
                    "sha256": "b" * 64, "size": 10, "entries": 2, "bytes": 4, "verified": True,
                    "application": {"restarted": True, "running": True}},
                "orders on production", "backup")
        text = buffer.getvalue()
        self.assertIn("Outcome: succeeded", text)
        self.assertIn("Archive: shiplord-backup-orders-20240101T000000Z.tar.gz", text)
        self.assertIn("Sha256: " + "b" * 64, text)
        self.assertIn("Verified: yes", text)
        self.assertIn("Application: running", text)

    def test_deploy_validates_its_input_before_reading_any_server_alias(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)  # no server aliases exist here
            for args, expected in ((["--image", "ghcr.io/example/orders:latest", "--commit", "a" * 40], "immutable"),
                                   (["--image", "ghcr.io/example/orders@sha256:" + "a" * 64, "--commit", "main"], "40-character"),
                                   ([], "--image")):
                with self.subTest(args=args):
                    result = self.invoke(directory, "deploy", *args, "--json", env=env)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(expected, json.loads(result.stdout)["error"])

    def test_remote_commands_report_an_unknown_server_alias_as_a_prerequisite_failure(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            for command in (["status", "--json"], ["provision", "--json"], ["rollback", "--json"], ["restart", "--json"]):
                with self.subTest(command=command):
                    self.assertEqual(self.invoke(directory, *command, env=env).returncode, 3)
            self.assertEqual(self.invoke(directory, "server", "bootstrap", "production", env=env).returncode, 3)

    def test_deploy_requires_exactly_one_artifact_and_checks_it_against_the_manifest_runtime(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            script = Path(directory) / "app.sh"
            script.write_text("#!/bin/sh\n")
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            image = "ghcr.io/example/orders@sha256:" + "a" * 64
            cases = ((["--image", image, "--binary", str(script)], "exactly one"), ([], "exactly one"),
                     (["--binary", str(script), "--commit", "a" * 40], "Docker image"))
            for args, expected in cases:
                with self.subTest(args=args):
                    result = self.invoke(directory, "deploy", *args, "--json", env=env)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(expected, json.loads(result.stdout)["error"])
            native = Path(directory) / "native"
            native.mkdir()
            (native / "ship.toml").write_text(MANIFEST.replace('runtime = "docker"', 'runtime = "native"'))
            result = self.invoke(str(native), "deploy", "--binary", str(script), "--commit", "a" * 40, "--json", env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("not a Linux ELF", json.loads(result.stdout)["error"])
            result = self.invoke(str(native), "deploy", "--image", image, "--commit", "a" * 40, "--json", env=env)
            self.assertIn("native binary", json.loads(result.stdout)["error"])

    def test_registry_login_takes_the_token_only_from_a_file_or_stdin(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            result = self.invoke(directory, "registry", "login", "--username", "bot", "--token", "x", "--json", env=env)
            self.assertEqual(result.returncode, 2)  # there is no --token option at all
            for args in ([], ["--token-stdin", "--token-file", "f"]):
                result = self.invoke(directory, "registry", "login", "--username", "bot", *args, "--json", env=env)
                self.assertEqual(result.returncode, 2)
                self.assertIn("exactly one", json.loads(result.stdout)["error"])
            key = Path(directory) / "token"
            key.write_text("ghp_exampleTOKEN1234567890\n")
            result = self.invoke(directory, "registry", "login", "--username", "bad name", "--token-file", str(key), "--json", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("ghp_example", result.stdout + result.stderr)
            result = self.invoke(directory, "registry", "login", "--username", "bot", "--token-file", str(key), "--json", env=env)
            self.assertEqual(result.returncode, 3)  # valid input; no server alias exists in this sandbox
            result = self.invoke(directory, "registry", "login", "--username", "bot", "--token-file", str(key),
                                 "--registry", "docker.io", "--json", env=env)
            self.assertEqual(result.returncode, 3)  # Docker Hub is accepted like GHCR
            result = self.invoke(directory, "registry", "login", "--username", "bot", "--token-file", str(key),
                                 "--registry", "quay.io", "--json", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("ghcr.io and docker.io", json.loads(result.stdout)["error"])

    def test_env_edits_validate_before_any_server_lookup_and_never_echo_values(self):
        secret = "postgres://u:s3cr3t@db/x"
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            cases = ((["env", "set", f"PORT={secret}"], "managed by Shiplord"),
                     (["env", "set", f"BAD LINE {secret}"], "KEY=VALUE"),
                     (["env", "set", f"A={secret}\nB=2"], "invalid"),
                     (["env", "unset", "A B"], "variable name"),
                     (["env", "set"], "exactly one"),
                     (["env", "set", "A=1", "--from-file", "f"], "exactly one"))
            for args, expected in cases:
                with self.subTest(args=args[:2]):
                    result = self.invoke(directory, *args, "--json", env=env)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(expected, json.loads(result.stdout)["error"])
                    self.assertNotIn("s3cr3t", result.stdout + result.stderr)
            result = self.invoke(directory, "env", "set", f"DATABASE_URL={secret}", "--json", env=env)
            self.assertEqual(result.returncode, 3)  # valid input; no server alias exists in this sandbox
            self.assertNotIn("s3cr3t", result.stdout + result.stderr)
            self.assertEqual(self.invoke(directory, "env", "unset", "OLD", "--json", env=env).returncode, 3)
            env_file = Path(directory) / "runtime.env"
            env_file.write_text(f"DATABASE_URL={secret}\n")
            result = self.invoke(directory, "env", "set", "--from-file", str(env_file), "--json", env=env)
            self.assertEqual(result.returncode, 3)  # whole-file replacement still works
            self.assertNotIn("s3cr3t", result.stdout + result.stderr)

    def test_github_commands_need_a_manifest_and_a_server_alias(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            for command in (["github", "setup", "--repo", "QueryPlanner/orders"], ["github", "status"], ["github", "rotate-key"]):
                with self.subTest(command=command):
                    self.assertEqual(self.invoke(directory, *command, "--json", env=env).returncode, 2)  # no ship.toml
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            for command in (["github", "setup", "--repo", "QueryPlanner/orders"], ["github", "rotate-key"]):
                with self.subTest(command=command):
                    self.assertEqual(self.invoke(directory, *command, "--json", env=env).returncode, 3)  # alias unknown
            result = self.invoke(directory, "github", "setup", "--repo", "QueryPlanner/orders", "--tailscale-tags", "ci",
                                 "--json", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("tag:ci", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(directory).iterdir()), [Path(directory) / "ship.toml"])

    def test_github_setup_upgrade_workflow_refreshes_the_pins_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            project = Path(directory)
            (project / "ship.toml").write_text(MANIFEST)
            manifest = load_manifest(project / "ship.toml")
            texts = {DEPLOY_WORKFLOW_PATH: render_deploy_workflow(manifest, tooling_ref="ab" * 20),
                     WORKFLOW_PATH: render_workflow(manifest, tooling_ref="ab" * 20)}
            texts[DEPLOY_WORKFLOW_PATH] = texts[DEPLOY_WORKFLOW_PATH].replace(PINS["checkout"].commit, "9" * 40)
            for path, content in texts.items():
                (project / path).parent.mkdir(parents=True, exist_ok=True)
                (project / path).write_text(content + "# a local note\n")
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            result = self.invoke(directory, "github", "setup", "--upgrade-workflow", "--json", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["changed"], [DEPLOY_WORKFLOW_PATH])
            self.assertEqual(output["pins"], [{"action": "actions/checkout", "from": "9" * 40,
                                               "path": DEPLOY_WORKFLOW_PATH, "to": PINS["checkout"].commit,
                                               "version": "v4"}])
            refreshed = (project / DEPLOY_WORKFLOW_PATH).read_text()
            self.assertNotIn("9" * 40, refreshed)
            self.assertIn(str(PINS["checkout"]), refreshed)
            self.assertIn("# a local note", refreshed)
            self.assertEqual((project / WORKFLOW_PATH).read_text(), texts[WORKFLOW_PATH] + "# a local note\n")

    def test_github_setup_upgrade_workflow_refuses_a_workflow_shiplord_did_not_write(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            project = Path(directory)
            (project / "ship.toml").write_text(MANIFEST)
            hand_written = "name: Mine\non: push\njobs: {}\n"
            (project / DEPLOY_WORKFLOW_PATH).parent.mkdir(parents=True)
            (project / DEPLOY_WORKFLOW_PATH).write_text(hand_written)
            (project / WORKFLOW_PATH).write_text(hand_written)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            result = self.invoke(directory, "github", "setup", "--upgrade-workflow", "--json", env=env)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn(DEPLOY_WORKFLOW_PATH, json.loads(result.stdout)["error"])
            self.assertEqual((project / DEPLOY_WORKFLOW_PATH).read_text(), hand_written)
            self.assertEqual((project / WORKFLOW_PATH).read_text(), hand_written)
            mismatch = self.invoke(directory, "github", "setup", "--repo", "Other/repo", "--upgrade-workflow", "--json", env=env)
            self.assertEqual(mismatch.returncode, 2, mismatch.stdout)
            self.assertIn("already names", json.loads(mismatch.stdout)["error"])

    def test_github_setup_needs_a_repository_unless_it_only_upgrades_the_pins(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            result = self.invoke(directory, "github", "setup", "--json", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--repo", json.loads(result.stdout)["error"])
            result = self.invoke(directory, "github", "setup", "--upgrade-workflow", "--json", env=env)
            self.assertEqual(result.returncode, 2)  # there is no generated pipeline to upgrade yet
            self.assertIn("github setup", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(directory).iterdir()), [Path(directory) / "ship.toml"])

    def test_github_check_pins_reports_every_pin_and_keeps_the_token_out_of_the_output(self):
        token = "ghs_exampleTOKEN1234567890"
        seen = []

        def fetch(url, sent):
            seen.append((url, sent))
            return 200, "{}"
        with patch("shiplord.workflow.github_fetch", side_effect=fetch):
            result = CliRunner().invoke(cli.app, ["github", "check-pins", "--json"], env={"GITHUB_TOKEN": token})
        self.assertEqual(result.exit_code, 0, result.output)
        output = json.loads(result.stdout)
        self.assertEqual((output["ok"], output["checked"], output["missing"]), (True, len(PINS), []))
        self.assertEqual(len(seen), len(PINS))
        self.assertEqual({sent for _, sent in seen}, {token})
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_github_check_pins_without_the_api_fails_instead_of_reporting_success(self):
        with patch("shiplord.workflow.github_fetch", side_effect=OSError("no route to host")):
            result = CliRunner().invoke(cli.app, ["github", "check-pins", "--json"],
                                        env={"GITHUB_TOKEN": "", "GH_TOKEN": ""})
        self.assertEqual(result.exit_code, 1, result.output)
        output = json.loads(result.stdout)
        self.assertEqual((output["ok"], output["missing"]), (False, []))
        self.assertEqual(len(output["unverified"]), len(PINS))
        self.assertIn("nothing here says they resolve", output["error"])

    def test_github_setup_rejects_an_invalid_deployment_branch_before_using_github_or_a_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ship.toml").write_text(MANIFEST)
            result = self.invoke(directory, "github", "setup", "--repo", "QueryPlanner/orders",
                                 "--deployment-branch", "main..other", "--json")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("valid branch name", json.loads(result.stdout)["error"])
            self.assertEqual((root / "ship.toml").read_text(), MANIFEST)
            self.assertEqual(list(root.iterdir()), [root / "ship.toml"])

    def test_doctor_rejects_conflicting_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "doctor", "--local", "--server", "production", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_github_setup_creates_the_tailscale_credential_when_a_token_is_in_the_environment(self):
        token = "tskey-api-throwaway-fixture-value"
        manifest = load_manifest(Path(self.write_manifest()))
        requests = []

        def transport(url, headers, body):
            requests.append((url, headers, body))
            return 200, json.dumps({"id": "id123456", "audience": "api.tailscale.com/id123456"}).encode()

        fake_gh = Mock()
        fake_gh.oidc_subject_prefix.return_value = ("repo:QueryPlanner/orders", [])
        with tempfile.TemporaryDirectory() as config, \
                patch("shiplord.cli._manifest", return_value=manifest), \
                patch("shiplord.cli.load_server", return_value="appuser@server.example.com"), \
                patch("shiplord.cli._admin_target"), \
                patch("shiplord.cli.github.GitHub", return_value=fake_gh), \
                patch("shiplord.cli.github.github_setup", return_value={"ok": True}) as setup, \
                patch("shiplord.tailscale._https_post", side_effect=transport), \
                patch.dict(os.environ, {tailscale.API_TOKEN_ENV: token, "XDG_CONFIG_HOME": config}):
            result = CliRunner().invoke(cli.app, ["github", "setup", "--repo", "QueryPlanner/orders", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(token, result.output)
        self.assertEqual(json.loads(result.stdout)["tailscale"]["source"], "created through the Tailscale API")
        self.assertEqual(setup.call_args.kwargs["tailscale"],
                         {"SHIPLORD_TS_CLIENT_ID": "id123456", "SHIPLORD_TS_AUDIENCE": "api.tailscale.com/id123456"})
        payload = json.loads(requests[0][2])
        self.assertEqual(payload["subject"], "repo:QueryPlanner/*:ref:refs/heads/main")
        self.assertNotIn(token, json.dumps(payload))

    def test_github_setup_without_a_token_keeps_the_printed_instructions_and_makes_no_request(self):
        manifest = load_manifest(Path(self.write_manifest()))
        fake_gh = Mock()
        fake_gh.oidc_subject_prefix.return_value = ("repo:QueryPlanner/orders", [])
        with tempfile.TemporaryDirectory() as config, \
                patch("shiplord.cli._manifest", return_value=manifest), \
                patch("shiplord.cli.load_server", return_value="appuser@server.example.com"), \
                patch("shiplord.cli._admin_target"), \
                patch("shiplord.cli.github.GitHub", return_value=fake_gh), \
                patch("shiplord.cli.github.github_setup", return_value={"ok": True}) as setup, \
                patch("shiplord.tailscale._https_post", side_effect=AssertionError("must not call Tailscale")), \
                patch.dict(os.environ, {"XDG_CONFIG_HOME": config}, clear=False):
            os.environ.pop(tailscale.API_TOKEN_ENV, None)
            result = CliRunner().invoke(cli.app, ["github", "setup", "--repo", "QueryPlanner/orders", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["tailscale"]["source"], "not configured")
        self.assertEqual(setup.call_args.kwargs["tailscale"], {})

    def write_manifest(self) -> str:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "ship.toml"
        path.write_text(MANIFEST)
        return str(path)

    def test_configure_creates_a_valid_manifest_without_github_settings(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "orders"
            directory.mkdir()
            result = self.invoke(str(directory), "configure", "--server", "production", "--api-domain", "api.example.com", "--frontend", "https://example.com")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_manifest(directory / "ship.toml")
            self.assertEqual(config["name"], "orders")
            self.assertEqual(config["http"]["frontend_origins"], ["https://example.com"])
            self.assertNotIn("github", config)
            self.assertIn("has not been checked", result.stdout)
            # Without a terminal and without a GitHub remote there is nothing to infer, so say what provision needs.
            self.assertIn("--image-repository", result.stderr)

    def test_configure_records_the_image_repository_provision_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                 "--api-domain", "api.example.com", "--no-frontend",
                                 "--image-repository", "owner/orders")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_manifest(Path(directory) / "ship.toml")
            self.assertEqual(config["github"], {"repository": "owner/orders", "deployment_branch": "main"})
            self.assertEqual(image_repository_for(config, None), "ghcr.io/owner/orders")
            self.assertNotIn("warning", result.stderr)

    @unittest.skipUnless(shutil.which("git"), "git is needed to infer a repository")
    def test_configure_infers_the_image_repository_from_the_git_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            self.git(directory, "init", "-q")
            self.git(directory, "remote", "add", "origin", "git@github.com:owner/orders.git")
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                 "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_manifest(Path(directory) / "ship.toml")
            self.assertEqual(config["github"]["repository"], "owner/orders")
            self.assertEqual(image_repository_for(config, None), "ghcr.io/owner/orders")
            self.assertNotIn("warning", result.stderr)

    def test_configure_rejects_an_unusable_image_repository_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            for repository in ("orders", "../owner/orders", "owner/orders/tree/main"):
                with self.subTest(repository=repository):
                    result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                         "--api-domain", "api.example.com", "--no-frontend",
                                         "--image-repository", repository)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("owner/repository", result.stderr)
                    self.assertFalse((Path(directory) / "ship.toml").exists())

    def test_configure_refuses_an_image_repository_for_a_native_application(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                 "--api-domain", "api.example.com", "--no-frontend", "--runtime", "native",
                                 "--image-repository", "owner/orders")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--image-repository applies only to Docker applications", result.stderr)
            self.assertFalse((Path(directory) / "ship.toml").exists())

    def test_configure_without_a_terminal_warns_when_no_repository_is_known(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                 "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--image-repository", result.stderr)
            self.assertNotIn("github", load_manifest(Path(directory) / "ship.toml"))

    def test_configure_supports_an_explicit_empty_frontend(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_manifest(Path(directory) / "ship.toml")["http"]["frontend_origins"], [])

    def test_configure_generic_validates_before_creating_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com",
                    "--no-frontend", "--template", "generic"]
            rejected = self.invoke(directory, *args, "--runtime", "native")
            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
            accepted = self.invoke(directory, *args)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(load_manifest(Path(directory) / "ship.toml")["template"], "generic")

    def test_generic_github_setup_passes_the_profile_gate_and_stops_at_the_server_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ship.toml").write_text(MANIFEST.replace('template = "google-adk"', 'template = "generic"'))
            config_home = root / "config"
            result = self.invoke(directory, "github", "setup", "--repo", "QueryPlanner/orders",
                                 "--tailscale-client-id", "throwaway-fixture", "--tailscale-audience",
                                 "api.tailscale.com/throwaway-fixture", "--json",
                                 env=dict(os.environ, XDG_CONFIG_HOME=str(config_home)))
            self.assertEqual(result.returncode, 3, result.stderr)  # no such server alias in this sandbox
            self.assertNotIn("generic Docker applications", result.stdout)
            self.assertFalse((root / ".github").exists())
            self.assertEqual([p.name for p in root.iterdir() if p.name != "config"], ["ship.toml"])

    def test_github_setup_refuses_conflicting_quality_options_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ship.toml").write_text(MANIFEST)
            result = self.invoke(directory, "github", "setup", "--repo", "QueryPlanner/orders",
                                 "--quality-workflow", ".github/workflows/checks.yml", "--no-quality-workflow", "--json",
                                 env=dict(os.environ, XDG_CONFIG_HOME=str(root / "config")))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("either --quality-workflow or --no-quality-workflow", json.loads(result.stdout)["error"])
            self.assertEqual(list(root.iterdir()), [root / "ship.toml"])

    def test_keys_add_reads_one_validated_public_key_from_exactly_one_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=str(root / "config"))
            for args in ([], ["--public-key-file", "ci.pub", "--public-key-stdin"]):
                with self.subTest(args=args):
                    result = self.invoke(directory, "keys", "add", "--label", "ci", *args, "--json", env=env)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("exactly one", json.loads(result.stdout)["error"])
            broken = root / "broken.pub"
            broken.write_text("ssh-ed25519 AAAAnotarealkey\n")
            result = self.invoke(directory, "keys", "add", "--label", "ci", "--public-key-file", str(broken), "--json", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("AAAAnotarealkey", result.stdout + result.stderr)
            result = self.invoke(directory, "keys", "add", "--label", "Bad Label", "--public-key-file", str(broken),
                                 "--json", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("key label", json.loads(result.stdout)["error"])
            good = root / "ci.pub"
            good.write_text(public_key_line() + "\n")
            result = self.invoke(directory, "keys", "add", "--label", "ci", "--public-key-file", str(good), "--json", env=env)
            self.assertEqual(result.returncode, 3, result.stderr)  # valid input; no server alias exists here

    def test_keys_remove_needs_confirmation_and_keys_commands_need_a_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, XDG_CONFIG_HOME=str(root / "config"))
            for command in (["keys", "list"], ["keys", "remove", "--label", "ci"]):
                with self.subTest(command=command):
                    self.assertEqual(self.invoke(directory, *command, "--json", env=env).returncode, 2)  # no ship.toml
            (root / "ship.toml").write_text(MANIFEST)
            result = self.invoke(directory, "keys", "remove", "--label", "ci", "--json", env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--yes", json.loads(result.stdout)["error"])

    def test_configure_refuses_ambiguous_or_invalid_inputs(self):
        cases = [[], ["--frontend", "https://example.com", "--no-frontend"], ["--frontend", "https://example.com/path"]]
        with tempfile.TemporaryDirectory() as directory:
            for options in cases:
                with self.subTest(options=options):
                    result = self.invoke(directory, "configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com", *options)
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse((Path(directory) / "ship.toml").exists())

    def test_configure_never_replaces_an_existing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "ship.toml"
            manifest.write_text("existing user data")
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(manifest.read_text(), "existing user data")
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["ship.toml"])

    def test_configure_does_not_follow_manifest_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "user-data"
            target.write_text("keep this")
            (Path(directory) / "ship.toml").symlink_to(target)
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), "keep this")

    def test_configure_without_a_terminal_still_requires_server_and_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Dockerfile").write_text("FROM x\nEXPOSE 3000\n")
            for options in ([], ["--server", "production", "--no-frontend"], ["--api-domain", "api.example.com", "--no-frontend"]):
                with self.subTest(options=options):
                    result = self.invoke(directory, "configure", "--name", "orders", *options)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("in a terminal", result.stderr)
            self.assertFalse((Path(directory) / "ship.toml").exists())

    def test_configure_without_a_terminal_keeps_defaults_and_ignores_the_dockerfile(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Dockerfile").write_text("FROM x\nEXPOSE 3000\n")
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                 "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_manifest(Path(directory) / "ship.toml")
            self.assertEqual((config["template"], config["http"]["container_port"], config["http"]["readiness_path"],
                              config["resources"]["memory_mb"]), ("google-adk", 8080, "/ready", 512))

    def test_configure_writes_port_health_path_and_memory_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production",
                                 "--api-domain", "api.example.com", "--no-frontend", "--template", "generic",
                                 "--container-port", "3000", "--health-path", "/health", "--memory", "256")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_manifest(Path(directory) / "ship.toml")
            self.assertEqual(config["http"]["container_port"], 3000)
            self.assertEqual(config["http"]["readiness_path"], "/health")
            self.assertEqual(config["resources"]["memory_mb"], 256)
            other = Path(directory) / "other"
            other.mkdir()
            rejected = self.invoke(str(other), "configure", "--name", "orders", "--server", "production",
                                   "--api-domain", "api.example.com", "--no-frontend", "--health-path", "health")
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(list(other.iterdir()), [])

    def test_configure_reports_a_locally_registered_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            servers = Path(directory) / ".test-config/shiplord/servers"
            servers.mkdir(parents=True, mode=0o700)
            for path in (servers.parent.parent, servers.parent, servers):
                path.chmod(0o700)
            (servers / "production.json").write_text(json.dumps({"schema_version": 1, "alias": "production",
                                                                 "ssh": "admin@example.com"}))
            (servers / "production.json").chmod(0o600)
            project = Path(directory) / "orders"
            project.mkdir()
            env = dict(os.environ, XDG_CONFIG_HOME=str(Path(directory) / ".test-config"))
            result = self.invoke(str(project), "configure", "--server", "production", "--api-domain",
                                 "api.example.com", "--no-frontend", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("registered locally; SSH access was not checked", result.stdout)

    def test_doctor_local_reports_only_local_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "doctor", "--local", "--json")
        self.assertIn(result.returncode, (0, 3))
        self.assertIn("server readiness was not checked", json.loads(result.stdout)["scope"])

    def test_doctor_reports_missing_prerequisites(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PATH=directory)
            result = self.invoke(directory, "doctor", "--local", env=env)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ssh: missing", result.stdout)

    def test_doctor_outside_an_application_runs_the_local_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "doctor")
        self.assertIn(result.returncode, (0, 3))
        self.assertIn("Target: local machine", result.stdout)
        self.assertIn("Local tools:", result.stdout)
        self.assertNotIn("ship.toml was not found", result.stdout)

    def test_doctor_outside_an_application_reports_the_unchecked_scope_and_next_command(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "doctor")
        self.assertIn("Server readiness was not checked", result.stdout)
        self.assertIn("shiplord configure", result.stdout)
        self.assertIn("shiplord doctor --server <alias>", result.stdout)

    def test_doctor_outside_an_application_keeps_the_local_json_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            fallback = self.invoke(directory, "doctor", "--json")
            local = self.invoke(directory, "doctor", "--local", "--json")
        self.assertEqual(fallback.returncode, local.returncode, fallback.stderr)
        output = json.loads(fallback.stdout)
        self.assertEqual(set(output), {"ok", "scope", "checks", "client"})
        self.assertEqual(set(output), set(json.loads(local.stdout)))
        self.assertIn("server readiness was not checked", output["scope"])
        self.assertEqual(set(output["checks"]), {"ssh", "git", "gh"})

    def test_doctor_outside_an_application_exits_three_when_a_tool_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PATH=directory)
            result = self.invoke(directory, "doctor", env=env)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ssh: missing", result.stdout)

    def test_doctor_reports_an_invalid_manifest_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "ship.toml").write_text("this is not toml")
            result = self.invoke(directory, "doctor", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ship.toml", json.loads(result.stdout)["error"])

    def test_version_and_help_use_shiplord(self):
        with tempfile.TemporaryDirectory() as directory:
            version = self.invoke(directory, "--version")
            help_result = self.invoke(directory, "--help")
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), f"shiplord {__version__}")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("shiplord", help_result.stdout.lower())


class ServerConfigMixin(unittest.TestCase):
    def write_servers(self, config: Path, *servers: tuple[str, str]) -> None:
        directory = config / "shiplord" / "servers"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (config, directory.parent, directory):
            path.chmod(0o700)
        for alias, ssh in servers:
            path = directory / f"{alias}.json"
            path.write_text(json.dumps({"schema_version": 1, "alias": alias, "ssh": ssh}))
            path.chmod(0o600)

    def env(self, config: Path) -> dict[str, str]:
        return dict(os.environ, XDG_CONFIG_HOME=str(config))


class CLIInventoryTests(ServerConfigMixin):
    inventory = {"ok": True, "applications": [{
        "application": "orders", "domain": "orders.example.com", "runtime": "docker", "host_port": 23001,
        "release_recorded": True, "unfinished_transaction": None, "ok": True,
    }]}

    def setUp(self):
        probe = patch("shiplord.cli.probe_capabilities", return_value={"helper_version": "0.2.0"})
        probe.start()
        self.addCleanup(probe.stop)

    def test_confirmed_old_helper_reports_upgrade_without_admin_or_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.probe_capabilities", return_value={"helper_version": "0.1.0"}) as probe, \
                 patch("shiplord.cli.commands.server_applications") as inventory, \
                 patch("shiplord.cli.bootstrap_server") as bootstrap:
                result = CliRunner().invoke(cli.app, ["apps", "hetzner-vm", "--json"], env=self.env(config))
                text_result = CliRunner().invoke(cli.app, ["apps", "hetzner-vm"], env=self.env(config))
            self.assertEqual(result.exit_code, 4, result.output)
            self.assertEqual(json.loads(result.stdout), {
                "ok": False, "server": "hetzner-vm", "error": "App inventory requires helper 0.2.0 or later.",
                "error_code": "helper_upgrade_required", "helper_version": "0.1.0",
                "minimum_helper_version": "0.2.0", "upgrade_command": "shiplord server bootstrap hetzner-vm"})
            self.assertEqual(text_result.exit_code, 4)
            self.assertIn("`hetzner-vm` is running helper 0.1.0. App inventory requires helper 0.2.0 or later. "
                          "Run `shiplord server bootstrap hetzner-vm`, then retry `shiplord apps hetzner-vm`.", text_result.stdout)
            self.assertEqual(probe.call_count, 2)
            probe.assert_called_with("admin@host.example.com", timeout=5)
            inventory.assert_not_called()
            bootstrap.assert_not_called()

    def test_inventory_falls_back_for_inconclusive_probes_and_accepts_new_helpers(self):
        from shiplord.transport import TransportError
        from shiplord.protocol import IncompatibleProtocol

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            for probe_result in ({"helper_version": "0.2.0"}, {"helper_version": "1.10.0"},
                                 {"helper_version": "0.1.0-rc.1"}, {"helper_version": "unknown"},
                                 TransportError("timed out"), IncompatibleProtocol("incompatible")):
                with self.subTest(probe=probe_result):
                    kwargs = {"side_effect": probe_result} if isinstance(probe_result, Exception) else {"return_value": probe_result}
                    with patch("shiplord.cli.probe_capabilities", **kwargs) as probe, \
                         patch("shiplord.cli.sys.stdin.isatty", return_value=True), \
                         patch("shiplord.cli.sys.stderr.isatty", return_value=True), \
                         patch("shiplord.cli.commands.server_applications", return_value=(0, self.inventory)) as inventory:
                        result = CliRunner().invoke(cli.app, ["apps", "alpha", "--json"], env=self.env(config))
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(json.loads(result.stdout)["applications"], self.inventory["applications"])
                    probe.assert_called_once_with("admin@alpha.example.com", timeout=5)
                    self.assertIsNone(inventory.call_args.args[0].prompt)
            with patch("shiplord.cli.probe_capabilities", side_effect=TransportError("timed out")), \
                 patch("shiplord.cli.commands.server_applications", side_effect=ClientError("invalid request", 2, error_code="invalid_request")):
                result = CliRunner().invoke(cli.app, ["apps", "alpha", "--json"], env=self.env(config))
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(json.loads(result.stdout)["error_code"], "invalid_request")

    def test_all_servers_collects_old_and_current_helper_results(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"), ("beta", "admin@beta.example.com"))
            def probe(target, timeout):
                return {"helper_version": "0.1.0" if target.startswith("admin@alpha") else "0.2.0"}
            with patch("shiplord.cli.probe_capabilities", side_effect=probe) as probes, \
                 patch("shiplord.cli.commands.server_applications", return_value=(0, self.inventory)) as inventory:
                result = CliRunner().invoke(cli.app, ["apps", "--all-servers", "--json"], env=self.env(config))
            self.assertEqual(result.exit_code, 3, result.output)
            data = json.loads(result.stdout)
            self.assertTrue(data["partial"])
            self.assertEqual(data["servers"][0]["error_code"], "helper_upgrade_required")
            self.assertEqual(data["servers"][1]["applications"], self.inventory["applications"])
            self.assertEqual(probes.call_count, 2)
            self.assertEqual(inventory.call_count, 1)

    def test_doctor_all_servers_reports_a_mixed_fleet_and_keeps_probing(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"),
                               ("beta", "admin@beta.example.com"), ("gamma", "admin@gamma.example.com"))

            def probe(target, timeout):
                if target.startswith("admin@beta"):
                    raise TransportError("SSH capabilities check timed out")
                return {"helper_version": "0.2.0" if target.startswith("admin@alpha") else "0.1.0"}

            with patch("shiplord.commands.probe_capabilities", side_effect=probe) as probes:
                result = CliRunner().invoke(cli.app, ["doctor", "--all-servers", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 3, result.output)
        data = json.loads(result.stdout)
        self.assertEqual((data["ok"], data["partial"]), (False, True))
        self.assertEqual([item["server"] for item in data["servers"]], ["alpha", "beta", "gamma"])
        self.assertEqual(probes.call_count, 3)  # the failed probe did not stop the sweep
        alpha, beta, gamma = data["servers"]
        self.assertEqual((alpha["reachable"], alpha["helper_version"], alpha["inventory_support"]),
                         (True, "0.2.0", "supported"))
        self.assertNotIn("upgrade_command", alpha)
        self.assertEqual((beta["reachable"], beta["inventory_support"]), (False, "unknown"))
        self.assertNotIn("upgrade_command", beta)
        self.assertEqual((gamma["reachable"], gamma["helper_version"], gamma["inventory_support"]),
                         (True, "0.1.0", "unsupported"))
        self.assertEqual(gamma["upgrade_command"], "shiplord server bootstrap gamma")
        self.assertEqual(data["client"]["version"], __version__)

    def test_doctor_all_servers_human_output_hints_only_the_confirmed_old_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"),
                               ("beta", "admin@beta.example.com"), ("gamma", "admin@gamma.example.com"))

            def probe(target, timeout):
                if target.startswith("admin@beta"):
                    raise TransportError("SSH capabilities check timed out")
                return {"helper_version": "0.2.0" if target.startswith("admin@alpha") else "0.1.0"}

            with patch("shiplord.commands.probe_capabilities", side_effect=probe):
                result = CliRunner().invoke(cli.app, ["doctor", "--all-servers"], env=self.env(config))
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        self.assertEqual(result.exit_code, 3, output)
        self.assertIn("alpha: reachable, helper 0.2.0, inventory supported", output)
        self.assertIn("beta: unreachable, inventory unknown", output)
        self.assertIn("gamma: reachable, helper 0.1.0, inventory unsupported", output)
        self.assertIn("shiplord server bootstrap gamma", output)
        self.assertNotIn("server bootstrap alpha", output)
        self.assertNotIn("server bootstrap beta", output)

    def test_doctor_all_servers_exits_zero_only_for_a_complete_compatible_fleet(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"), ("beta", "admin@beta.example.com"))
            with patch("shiplord.commands.probe_capabilities", return_value={"helper_version": "0.2.0"}):
                result = CliRunner().invoke(cli.app, ["doctor", "--all-servers", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.stdout)
        self.assertEqual((data["ok"], data["partial"]), (True, False))
        self.assertEqual([item["server"] for item in data["servers"]], ["alpha", "beta"])

    def test_doctor_all_servers_rejects_conflicting_modes_before_connecting(self):
        for extra in (["--server", "alpha"], ["--local"], ["--deep"]):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                config = Path(temp) / "config"
                with patch("shiplord.commands.probe_capabilities") as probe, \
                     patch("shiplord.cli.bootstrap_server") as bootstrap, \
                     patch("shiplord.cli.getpass.getpass") as prompt:
                    result = CliRunner().invoke(cli.app, ["doctor", "--all-servers", *extra, "--json"],
                                                env=self.env(config))
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertFalse(json.loads(result.stdout)["ok"])
                probe.assert_not_called()
                bootstrap.assert_not_called()
                prompt.assert_not_called()
                self.assertFalse(config.exists())

    def test_doctor_all_servers_never_asks_for_sudo_or_runs_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            with patch("shiplord.commands.probe_capabilities", return_value={"helper_version": "0.1.0"}), \
                 patch("shiplord.cli.bootstrap_server") as bootstrap, \
                 patch("shiplord.cli._admin_target") as admin, \
                 patch("shiplord.cli.getpass.getpass") as prompt:
                result = CliRunner().invoke(cli.app, ["doctor", "--all-servers"], env=self.env(config))
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertIn("server bootstrap alpha", result.output)  # guidance only, never executed
        bootstrap.assert_not_called()
        admin.assert_not_called()
        prompt.assert_not_called()

    def test_doctor_all_servers_counts_unreadable_registrations_as_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            bad = config / "shiplord" / "servers" / "bad.json"
            bad.write_text("invalid")
            bad.chmod(0o600)
            with patch("shiplord.commands.probe_capabilities", return_value={"helper_version": "0.2.0"}):
                result = CliRunner().invoke(cli.app, ["doctor", "--all-servers", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 3, result.output)
        data = json.loads(result.stdout)
        self.assertEqual((data["ok"], data["partial"], data["invalid_entries"]), (False, True, 1))
        self.assertEqual([item["server"] for item in data["servers"]], ["alpha"])

    def test_doctor_all_servers_requires_a_registered_server(self):
        with tempfile.TemporaryDirectory() as temp:
            result = CliRunner().invoke(cli.app, ["doctor", "--all-servers", "--json"],
                                        env=self.env(Path(temp) / "config"))
        self.assertEqual(result.exit_code, 3)
        self.assertIn("server add", json.loads(result.stdout)["error"])

    def test_single_server_doctor_is_unchanged_for_an_old_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            with patch("shiplord.cli.probe_capabilities", return_value={"helper_version": "0.1.0"}) as probe:
                result = CliRunner().invoke(cli.app, ["doctor", "--server", "alpha", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.stdout)
        self.assertEqual((data["ok"], data["helper_version"]), (True, "0.1.0"))
        self.assertNotIn("partial", data)
        probe.assert_called_once_with("admin@alpha.example.com")  # still the single-server probe and default deadline

    def write_servers(self, config: Path, *servers: tuple[str, str]) -> None:
        directory = config / "shiplord" / "servers"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (config, directory.parent, directory):
            path.chmod(0o700)
        for alias, ssh in servers:
            path = directory / f"{alias}.json"
            path.write_text(json.dumps({"schema_version": 1, "alias": alias, "ssh": ssh}))
            path.chmod(0o600)

    def env(self, config: Path) -> dict[str, str]:
        return dict(os.environ, XDG_CONFIG_HOME=str(config))

    def test_provision_update_passes_the_flag_through(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            project = Path(temp) / "project"
            project.mkdir()
            (project / "ship.toml").write_text(MANIFEST)
            self.write_servers(config, ("production", "admin@host.example.com"))
            original = os.getcwd()
            self.addCleanup(os.chdir, original)
            os.chdir(project)
            with patch("shiplord.cli.commands.provision_application", return_value=(0, {"ok": True})) as provision:
                result = CliRunner().invoke(cli.app, ["provision", "--update", "--json"], env=self.env(config))
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(provision.call_args.kwargs["update"])
            with patch("shiplord.cli.commands.provision_application", return_value=(0, {"ok": True})) as provision:
                result = CliRunner().invoke(cli.app, ["provision", "--json"], env=self.env(config))
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertFalse(provision.call_args.kwargs["update"])
    def test_doctor_deep_prints_ignored_registry_files_once_and_keeps_the_json_field(self):
        applications = [{"application": "orders", "deployed": True, "unfinished_transaction": None}]
        ignored = {"ok": True, "checks": {"applications": {"ok": True, "applications": applications,
                                                          "ignored_files": ["orders.json~"]}}}
        clean = {"ok": True, "checks": {"applications": {"ok": True, "applications": applications,
                                                        "ignored_files": []}}}
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.commands.server_doctor", return_value=(0, ignored)):
                text = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--deep"], env=self.env(config))
                payload = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--deep", "--json"],
                                             env=self.env(config))
            with patch("shiplord.cli.commands.server_doctor", return_value=(0, clean)):
                quiet = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--deep"], env=self.env(config))
        self.assertEqual(text.exit_code, 0, text.output)
        self.assertEqual(text.stdout.count("orders.json~"), 1)
        self.assertIn("Ignored 1 file", text.stdout)
        self.assertNotIn(str(Path(temp)), text.stdout)
        self.assertEqual(json.loads(payload.stdout)["checks"]["applications"]["ignored_files"], ["orders.json~"])
        self.assertEqual(quiet.exit_code, 0, quiet.output)
        self.assertNotIn("Ignored", quiet.stdout)

    def test_doctor_deep_renders_the_os_baseline_with_the_detected_release(self):
        refused = {"ok": False, "checks": {"os": {
            "ok": False, "id": "ubuntu", "version_id": "22.04",
            "message": "unsupported server operating system: ubuntu 22.04 is older than the supported minimum; "
                       "Shiplord supports Ubuntu 24.04 or later and Debian 12 or later"}}}
        supported = {"ok": True, "checks": {"os": {"ok": True, "id": "debian", "version_id": "12"}}}
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.commands.server_doctor", return_value=(3, refused)):
                failed = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--deep"],
                                            env=self.env(config))
            with patch("shiplord.cli.commands.server_doctor", return_value=(0, supported)):
                passed = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--deep"],
                                            env=self.env(config))
        self.assertEqual(failed.exit_code, 3, failed.output)
        self.assertIn("Server baseline:", failed.stdout)
        self.assertIn("os: failed (id: ubuntu; version id: 22.04; message: unsupported server operating system:",
                      failed.stdout)
        self.assertEqual(passed.exit_code, 0, passed.output)
        self.assertIn("os: passed (id: debian; version id: 12)", passed.stdout)

    def test_apps_uses_a_single_server_alias_and_hides_the_port_from_the_table(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            runner = CliRunner()
            with patch("shiplord.cli.commands.server_applications", return_value=(0, self.inventory)) as inventory:
                result = runner.invoke(cli.app, ["apps"], env=self.env(config))
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Server: hetzner-vm", result.output)
            self.assertIn("orders.example.com", result.output)
            self.assertNotIn("23001", result.output)
            self.assertEqual(inventory.call_args.args[0].target, "admin@host.example.com")

    def test_apps_json_keeps_host_port_and_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.commands.server_applications", return_value=(0, self.inventory)):
                result = CliRunner().invoke(cli.app, ["apps", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.stdout)
        self.assertEqual(data["server"], "hetzner-vm")
        self.assertEqual(data["applications"][0]["host_port"], 23001)

    def test_apps_lists_aliases_when_server_selection_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"), ("beta", "admin@beta.example.com"))
            result = CliRunner().invoke(cli.app, ["apps", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(json.loads(result.stdout)["aliases"], ["alpha", "beta"])

    def test_apps_reports_unattended_sudo_authorization_as_structured_json(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            with patch("shiplord.cli.commands.server_applications",
                       side_effect=ClientError("sudo authorization is required", 3,
                                               error_code="authorization_required")):
                result = CliRunner().invoke(cli.app, ["apps", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(json.loads(result.stdout)["error_code"], "authorization_required")

    def test_all_servers_marks_unreachable_aliases_as_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"), ("beta", "admin@beta.example.com"))

            def inventory(admin):
                if admin.target.startswith("admin@beta"):
                    raise ClientError("the SSH exchange timed out", 3)
                return 0, self.inventory

            with patch("shiplord.cli.commands.server_applications", side_effect=inventory):
                result = CliRunner().invoke(cli.app, ["apps", "--all-servers", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 3)
        data = json.loads(result.stdout)
        self.assertTrue(data["partial"])
        self.assertEqual([item["server"] for item in data["servers"]], ["alpha", "beta"])
        self.assertFalse(data["servers"][1]["ok"])

    def test_inventory_reports_invalid_records_and_unreadable_state(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            bad = config / "shiplord" / "servers" / "bad.json"
            bad.write_text("invalid")
            bad.chmod(0o600)
            with patch("shiplord.cli.commands.server_applications", return_value=(0, self.inventory)):
                result = CliRunner().invoke(cli.app, ["apps", "--all-servers", "--json"], env=self.env(config))
            self.assertEqual(result.exit_code, 3)
            self.assertTrue(json.loads(result.stdout)["partial"])
            for inventory in ({"ok": False, "applications": [], "error": "application registry is unreadable"},
                              {"ok": False, "applications": [{**self.inventory["applications"][0], "ok": False,
                               "release_recorded": None, "error": "application state is unreadable"}]}):
                with patch("shiplord.cli.commands.server_applications", return_value=(3, inventory)):
                    result = CliRunner().invoke(cli.app, ["apps", "alpha"], env=self.env(config))
                self.assertIn("unreadable", result.stdout)
                self.assertNotIn("No applications registered", result.stdout)
                if inventory["applications"]:
                    self.assertIn("unknown", result.stdout)

    def test_old_helper_inventory_rejection_gives_upgrade_guidance(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("alpha", "admin@alpha.example.com"))
            with patch("shiplord.cli.commands.server_applications", side_effect=ClientError("invalid request", 2, error_code="invalid_request")):
                for args in (["apps", "alpha", "--json"], ["apps", "--all-servers", "--json"]):
                    result = CliRunner().invoke(cli.app, args, env=self.env(config))
                    self.assertIn("doctor --server alpha --deep", result.stdout)
                    self.assertIn("server bootstrap alpha", result.stdout)

    def test_interactive_json_can_prompt_for_sudo(self):
        with patch("shiplord.cli.sys.stdin.isatty", return_value=True), patch("shiplord.cli.sys.stderr.isatty", return_value=True):
            self.assertIsNotNone(cli._interactive_admin_prompt(True))

    def test_deployment_displays_release_and_warnings(self):
        result = {"ok": True, "status": "succeeded", "release": {"commit": "abc", "artifact": "image:tag"},
                  "probes": {"warning": "routing unverified"}, "warnings": ["cleanup failed"]}
        output = self.invoke_deploy(0, result)
        for value in ("abc", "image:tag", "routing unverified", "cleanup failed"):
            self.assertIn(value, output.stdout)

    def test_named_app_uses_app_scoped_ssh_and_requires_a_server_when_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch.dict(os.environ, self.env(config), clear=True):
                application, alias, client = cli._app_context("orders", None, False, "status")
            self.assertEqual((application, alias, client.target), ("orders", "hetzner-vm", "ship-orders@host.example.com"))
            self.write_servers(config, ("second", "admin@second.example.com"))
            with patch.dict(os.environ, self.env(config), clear=True):
                ambiguous = CliRunner().invoke(cli.app, ["status", "orders", "--json"])
            self.assertEqual(ambiguous.exit_code, 3)
            self.assertEqual(json.loads(ambiguous.stdout)["aliases"], ["hetzner-vm", "second"])

    def test_named_app_rejects_an_environment_target_for_a_different_app(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            env = dict(self.env(config), SHIPLORD_SSH_TARGET="ship-billing@host.example.com")
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(CommandError) as caught:
                    cli._app_context("orders", "hetzner-vm", False, "status")
        self.assertEqual(caught.exception.code, 3)

    def test_status_and_logs_accept_a_named_app_and_keep_status_json_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            status_result = {"ok": True, "application": {
                "application_id": "orders", "current": {"commit": "a" * 40, "artifact": "image:tag"},
                "previous": None, "unfinished_transaction": None, "running": True,
            }}
            with patch("shiplord.cli.commands.application_status", return_value=(0, status_result)) as status_call:
                status = CliRunner().invoke(cli.app, ["status", "orders", "--server", "hetzner-vm"],
                                            env=self.env(config))
            self.assertEqual(status.exit_code, 0, status.output)
            self.assertIn("Target: orders on hetzner-vm", status.output)
            self.assertIn("Outcome: running", status.output)
            self.assertEqual(status_call.call_args.args[0].target, "ship-orders@host.example.com")
            with patch("shiplord.cli.commands.application_status", return_value=(0, status_result)):
                status_json = CliRunner().invoke(cli.app, ["status", "orders", "--server", "hetzner-vm", "--json"],
                                                 env=self.env(config))
            self.assertEqual(set(json.loads(status_json.stdout)), {"ok", "application"})
            with patch("shiplord.cli.commands.application_logs", return_value=(0, {"ok": True, "logs": "one\n"})) as logs_call:
                logs = CliRunner().invoke(cli.app, ["logs", "orders", "--server", "hetzner-vm"], env=self.env(config))
            self.assertEqual(logs.exit_code, 0, logs.output)
            self.assertEqual(logs.stdout, "one\n")
            self.assertIn("Target: orders on hetzner-vm", logs.stderr)
            self.assertIn("one", logs.output)
            self.assertEqual(logs_call.call_args.args[0].target, "ship-orders@host.example.com")
            with patch("shiplord.cli.commands.application_logs", return_value=(0, {"ok": True, "logs": "one\n"})):
                logs_json = CliRunner().invoke(cli.app, ["logs", "orders", "--server", "hetzner-vm", "--json"],
                                               env=self.env(config))
            self.assertEqual(set(json.loads(logs_json.stdout)), {"ok", "logs"})

    def test_logs_forward_since_timestamps_and_follow_to_the_command(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.commands.application_logs",
                       return_value=(0, {"ok": True, "logs": "one\n"})) as call:
                result = CliRunner().invoke(cli.app, ["logs", "orders", "--server", "hetzner-vm", "--tail", "25",
                                                      "--since", "15m", "--timestamps"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(call.call_args.args[1], 25)
        self.assertEqual(call.call_args.kwargs["since"], "15m")
        self.assertTrue(call.call_args.kwargs["timestamps"])
        self.assertFalse(call.call_args.kwargs["follow"])

    def test_follow_and_json_are_refused_before_any_connection(self):
        with patch("shiplord.cli._app_context") as context:
            result = CliRunner().invoke(cli.app, ["logs", "orders", "--follow", "--json"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("cannot be combined", json.loads(result.stdout)["error"])
        context.assert_not_called()

    def test_follow_writes_chunks_as_they_arrive_and_reports_the_stop_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))

            def stream(client, tail, **options):
                options["on_chunk"]("one\n")
                options["on_chunk"]("two\n")
                return 0, {"ok": True, "logs": "", "stopped": "timeout", "release": "a" * 12}

            with patch("shiplord.cli.commands.application_logs", side_effect=stream) as call:
                result = CliRunner().invoke(cli.app, ["logs", "orders", "--server", "hetzner-vm", "--follow",
                                                      "--timeout", "60"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout, "one\ntwo\n")
        self.assertIn(f"Outcome: stopped at the timeout (release {'a' * 12})", result.stderr)
        self.assertEqual(call.call_args.kwargs["timeout"], 60)

    def test_ctrl_c_stops_following_with_exit_130(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.commands.application_logs", side_effect=KeyboardInterrupt):
                result = CliRunner().invoke(cli.app, ["logs", "orders", "--server", "hetzner-vm", "--follow"],
                                            env=self.env(config))
        self.assertEqual(result.exit_code, 130)
        self.assertIn("Stopped following logs.", result.stderr)


    def test_status_explains_an_observed_unfinished_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            status_result = {"ok": True, "application": {
                "application_id": "orders", "current": None, "previous": None,
                "unfinished_transaction": {"request_id": "req-1", "phase": "prepared"}, "running": None,
            }}
            with patch("shiplord.cli.commands.application_status", return_value=(0, status_result)):
                result = CliRunner().invoke(cli.app, ["status", "orders", "--server", "hetzner-vm"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Outcome: recovery required", result.output)
        self.assertIn("unfinished deployment transaction", result.output)
        self.assertIn("shiplord recover orders --server hetzner-vm", result.output)

    def test_status_hint_from_a_manifest_names_the_resolved_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("production", "admin@host.example.com"))
            project = Path(temp) / "project"
            project.mkdir()
            (project / "ship.toml").write_text(MANIFEST)
            status_result = {"ok": True, "application": {
                "application_id": "orders", "current": None, "previous": None,
                "unfinished_transaction": {"request_id": "req-1", "phase": "prepared"}, "running": None,
            }}
            current = Path.cwd()
            try:
                os.chdir(project)
                with patch("shiplord.cli.commands.application_status", return_value=(0, status_result)), \
                        patch("shiplord.cli._helper"):
                    result = CliRunner().invoke(cli.app, ["status"], env=self.env(config))
            finally:
                os.chdir(current)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("shiplord recover orders --server production", result.output)

    @staticmethod
    def empty_application() -> dict:
        return {"application_id": "orders", "current": None, "previous": None,
                "unfinished_transaction": None, "running": None}

    def invoke_status_job(self, status_result: dict, job_id: str = "req-1", *, as_json: bool = False):
        """Run `status --job-id` against a helper that records every request frame it receives."""
        calls = []

        def runner(command, payload, timeout):
            message = read_frame(io.BytesIO(payload))
            calls.append(message)
            buffer = io.BytesIO()
            write_frame(buffer, {"status": "ok", "request_id": message["request_id"], "result": status_result})
            return buffer.getvalue(), b"", 0

        client = HelperClient("orders", "ship-orders@host.example.com", runner=runner)
        arguments = ["status", "orders", "--server", "hetzner-vm", "--job-id", job_id]
        if as_json:
            arguments.append("--json")
        with patch("shiplord.cli._app_context", return_value=("orders", "hetzner-vm", client)):
            return CliRunner().invoke(cli.app, arguments), calls

    def test_status_job_lookup_uses_only_the_read_only_status_action(self):
        job = {"request_id": "req-1", "action": "deploy", "state": "running", "phase": "preparing",
               "submitted_at": 1, "started_at": 2, "finished_at": None, "outcome": None}
        result, calls = self.invoke_status_job({"application": self.empty_application(), "job": job})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([message["action"] for message in calls], ["status"])
        self.assertEqual(calls[0]["application_id"], "orders")
        self.assertEqual(calls[0]["arguments"], {"job_id": "req-1"})
        self.assertIn("Job: req-1", result.stdout)
        self.assertIn("Job state: running", result.stdout)
        self.assertIn("Job phase: preparing", result.stdout)

    def test_finished_job_reports_rollback_and_json_keeps_the_helper_record(self):
        job = {"request_id": "req-1", "action": "deploy", "state": "finished", "phase": "finished",
               "submitted_at": 1, "started_at": 2, "finished_at": 3,
               "outcome": {"status": "rolled_back", "exit_code": 5, "detail": {"error": "readiness_failed"}}}
        result, _ = self.invoke_status_job({"application": self.empty_application(), "job": job})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Job state: finished", result.stdout)
        self.assertIn("Job outcome: rolled_back (exit 5)", result.stdout)
        as_json, _ = self.invoke_status_job({"application": self.empty_application(), "job": job}, as_json=True)
        self.assertEqual(as_json.exit_code, 0, as_json.output)
        self.assertEqual(json.loads(as_json.stdout)["job"], job)

    def test_unknown_job_id_is_a_clear_nonzero_error(self):
        result, calls = self.invoke_status_job({"application": self.empty_application(), "job": None})
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("no job 'req-1' is recorded for orders on hetzner-vm", result.stdout)
        self.assertIn("may have expired", result.stdout)
        self.assertEqual(len(calls), 1)  # the read-only lookup was attempted before reporting it

    def test_malformed_job_id_is_rejected_before_any_remote_request(self):
        result, calls = self.invoke_status_job({"application": self.empty_application(), "job": None},
                                               job_id="../etc/passwd")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--job-id", result.stdout)
        self.assertEqual(calls, [])

    def invoke_remote_plan(self, plan_result: dict, *, as_json: bool = False):
        arguments = ["plan", "--image", "ghcr.io/example/orders@sha256:" + "a" * 64, "--commit", "b" * 40]
        if as_json:
            arguments.append("--json")
        with patch("shiplord.cli._manifest", return_value={"name": "orders", "server": "hetzner-vm"}), \
                patch("shiplord.cli._helper"), \
                patch("shiplord.cli.commands.validate_deploy_inputs"), \
                patch("shiplord.cli.commands.commit_sequence", return_value=1001), \
                patch("shiplord.cli.commands.plan_application", return_value=(0, plan_result)):
            return CliRunner().invoke(cli.app, arguments)

    def invoke_deploy(self, code: int, deploy_result: dict, *, as_json: bool = False):
        arguments = ["deploy", "--image", "ghcr.io/example/orders@sha256:" + "a" * 64, "--commit", "b" * 40]
        if as_json:
            arguments.append("--json")
        with patch("shiplord.cli._manifest", return_value={"name": "orders", "server": "hetzner-vm"}), \
                patch("shiplord.cli._helper"), \
                patch("shiplord.cli.commands.validate_deploy_inputs"), \
                patch("shiplord.cli.commands.commit_sequence", return_value=1001), \
                patch("shiplord.cli.commands.deploy_application", return_value=(code, deploy_result)):
            return CliRunner().invoke(cli.app, arguments)

    def test_recover_force_stop_reaches_the_command_and_help_lists_it(self):
        recovered = {"ok": True, "request_id": "rc-1", "status": "recovered", "exit_code": 0,
                     "running": False, "forced_stop": {"forced": True, "signal": "SIGKILL"}}
        with patch("shiplord.cli._manifest", return_value={"name": "orders", "server": "hetzner-vm"}), \
                patch("shiplord.cli._helper"), \
                patch("shiplord.cli.commands.run_job_command", return_value=(0, recovered)) as run:
            result = CliRunner().invoke(cli.app, ["recover", "--force-stop", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(run.call_args.kwargs["force_stop"])
        self.assertEqual(run.call_args.args[2], "recover")
        plain = CliRunner().invoke(cli.app, ["recover", "--help"], color=True)
        self.assertIn("--force-stop", re.sub(r"\x1b\[[0-9;]*m", "", plain.output))

    def test_restart_reaches_the_command_and_help_lists_it(self):
        restarted = {"ok": True, "request_id": "rs-1", "status": "succeeded", "exit_code": 0,
                     "release": {"commit": "a" * 40, "sequence": 1000}}
        with patch("shiplord.cli._manifest", return_value={"name": "orders", "server": "hetzner-vm"}), \
                patch("shiplord.cli._helper"), \
                patch("shiplord.cli.commands.run_job_command", return_value=(0, restarted)) as run:
            result = CliRunner().invoke(cli.app, ["restart", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.args[2], "restart")
        self.assertIn("succeeded", json.loads(result.stdout)["status"])
        plain = CliRunner().invoke(cli.app, ["--help"], color=True)
        self.assertIn("restart", re.sub(r"\x1b\[[0-9;]*m", "", plain.output))

    def test_plan_renders_release_route_storage_and_shutdown_changes(self):
        plan_result = {
            "ok": True, "scope": "server plan; nothing was changed", "action": "deploy",
            "candidate": {"commit": "a" * 40, "sequence": 1001, "release_id": "candidate",
                          "artifact": "ghcr.io/example/orders@sha256:" + "a" * 64},
            "current": {"commit": "b" * 40, "sequence": 1000, "release_id": "current",
                        "artifact": "ghcr.io/example/orders@sha256:" + "b" * 64},
            "restarts_application": True, "route_change": "replace", "domain": "orders.example.com",
            "storage": {"current_path": "/app/src/.adk", "candidate_path": "/data", "same_volume": True,
                        "note": "the same volume is mounted at the candidate path; no files are moved"},
            "shutdown": {"outgoing_policy": {"timeout_seconds": 10, "action": "fail"},
                         "candidate_policy": {"timeout_seconds": 60, "action": "force"},
                         "note": "the outgoing container policy governs its stop"},
        }
        result = self.invoke_remote_plan(plan_result)
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.output.splitlines()
        self.assertEqual(lines[:2], ["Target: orders on hetzner-vm", "Outcome: ready"])
        self.assertIn("Plan action: deploy", result.output)
        self.assertIn("Current release: bbbbbbbbbbbb", result.output)
        self.assertIn("Candidate release: aaaaaaaaaaaa", result.output)
        self.assertIn("sequence 1001", result.output)
        self.assertIn("Restarts application: yes", result.output)
        self.assertIn("Route change: replace", result.output)
        self.assertIn("Storage path: /app/src/.adk to /data", result.output)
        self.assertIn("Application volume reused: yes", result.output)
        self.assertIn("no files are moved", result.output)
        self.assertIn("Outgoing shutdown policy: 10 seconds, fail", result.output)
        self.assertIn("Candidate shutdown policy: 60 seconds, force", result.output)
        json_result = self.invoke_remote_plan(plan_result, as_json=True)
        self.assertEqual(json_result.exit_code, 0, json_result.output)
        self.assertEqual(json.loads(json_result.stdout), plan_result)

    def test_plan_renderer_handles_unchanged_and_refused_responses(self):
        unchanged = {"ok": True, "action": "unchanged",
                     "current": {"commit": "a" * 40, "sequence": 1001, "artifact": "image:sha256"},
                     "candidate": {"commit": "a" * 40, "sequence": 1001, "artifact": "image:sha256"}}
        result = self.invoke_remote_plan(unchanged)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Outcome: no changes required", result.output)
        self.assertNotIn("Route change:", result.output)
        self.assertNotIn("Restarts application:", result.output)

        refused = {"ok": True, "action": "none", "reason": "failed", "error": "recovery_required",
                   "message": "an unfinished transaction exists; run recover first"}
        result = self.invoke_remote_plan(refused)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Outcome: recovery required", result.output)
        self.assertIn("Reason: recovery required", result.output)
        self.assertIn("shiplord recover", result.output)

    def test_deploy_shows_public_https_failures_without_changing_json_or_exit_codes(self):
        dns_failure = {"ok": True, "request_id": "req-1", "status": "succeeded", "exit_code": 0,
                       "release": {"commit": "b" * 40}, "url": "https://orders.example.com",
                       "public": {"ok": False, "status": None, "error": "dns_unresolved"}}
        result = self.invoke_deploy(0, dns_failure)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.splitlines()[:2], [
            "Target: orders on hetzner-vm", "Outcome: deployed; public HTTPS check failed",
        ])
        self.assertIn("Public HTTPS: failed (dns unresolved)", result.output)
        json_result = self.invoke_deploy(0, dns_failure, as_json=True)
        self.assertEqual(json_result.exit_code, 0, json_result.output)
        self.assertEqual(json.loads(json_result.stdout), dns_failure)

        required_failure = {**dns_failure, "ok": False, "exit_code": 1,
                            "error": "the deployment is live on the server, but the public HTTPS check failed",
                            "public": {"ok": False, "status": 503, "error": "unexpected_status"}}
        result = self.invoke_deploy(1, required_failure)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Public HTTPS: failed (HTTP 503; unexpected status)", result.output)
        self.assertIn("Error: the deployment is live on the server", result.output)

    def test_logs_without_a_manifest_explains_named_app_and_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            current = Path.cwd()
            try:
                os.chdir(temp)
                result = CliRunner().invoke(cli.app, ["logs", "--json"], env=self.env(Path(temp) / "config"))
            finally:
                os.chdir(current)
        self.assertEqual(result.exit_code, 2)
        message = json.loads(result.stdout)["error"]
        self.assertIn("shiplord logs <app> --server <alias>", message)
        self.assertIn("shiplord apps", message)

    def test_help_groups_commands_and_orders_the_quick_start(self):
        result = CliRunner().invoke(cli.app, ["--help"], color=True)
        self.assertEqual(result.exit_code, 0, result.output)
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        quick = output.index("Quick start")
        setup = output.index("Setup")
        troubleshooting = output.index("Troubleshooting")
        administration = output.index("Administration")
        self.assertLess(quick, setup)
        self.assertLess(setup, troubleshooting)
        self.assertLess(troubleshooting, administration)
        quick_help = output[quick:setup]
        positions = [quick_help.find(name + " ") for name in ("apps", "status", "logs", "plan", "deploy")]
        self.assertTrue(all(position >= 0 for position in positions), output)
        self.assertEqual(positions, sorted(positions), output)
        deploy_help = CliRunner().invoke(cli.app, ["deploy", "--help"], color=True)
        deploy_output = re.sub(r"\x1b\[[0-9;]*m", "", deploy_help.output)
        self.assertIn("Advanced options", deploy_output)
        self.assertIn("--allow-older", deploy_output)

    def test_doctor_reports_client_path_and_helper_version(self):
        local = CliRunner().invoke(cli.app, ["doctor", "--local", "--json"])
        local_data = json.loads(local.stdout)
        self.assertEqual(local_data["client"]["version"], __version__)
        self.assertTrue(local_data["client"]["executable"])
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.probe_capabilities", return_value={"helper_version": "0.2.0", "actions": ["status"]}):
                server = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--json"], env=self.env(config))
        server_data = json.loads(server.stdout)
        self.assertEqual(server_data["helper_version"], "0.2.0")
        self.assertEqual(server_data["client"]["version"], __version__)


class ServerUninstallTests(ServerConfigMixin):
    """`shiplord server uninstall` calls the admin action and reports what it removed."""

    result = {"status": "uninstalled",
              "removed": {"caddy": {"import": True, "placeholder": True}, "sshd": True,
                          "wrappers": ["shiplord-host", "shiplord-admin"], "versions": 2,
                          "directories": ["registrations", "jobs"]},
              "retained": ["the state directory that holds the host lock"], "applications": []}

    def fake(self, result=None, error=None):
        calls = []

        class FakeAdmin:
            def __init__(self, target, prompt=None):
                self.target = target

            def call(self, action, application_id, arguments, *, timeout=120):
                calls.append((action, application_id, arguments, timeout))
                if error is not None:
                    raise error
                return dict(self_result if self_result is not None else ServerUninstallTests.result)

        self_result = result
        return FakeAdmin, calls

    def test_human_output_lists_removals_retained_state_and_the_data_warning(self):
        admin, calls = self.fake()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.AdminClient", admin):
                result = CliRunner().invoke(cli.app, ["server", "uninstall", "hetzner-vm"], env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls, [("uninstall", None, {"force": False}, 300)])
        self.assertIn("Removed:", result.stdout)
        self.assertIn("the managed Caddy import", result.stdout)
        self.assertIn("the launchers: shiplord-host, shiplord-admin", result.stdout)
        self.assertIn("the helper code (2 versions)", result.stdout)
        self.assertIn("Left in place:", result.stdout)
        self.assertIn("Application data is never deleted", result.stdout)
        self.assertIn("shiplord server bootstrap", result.stdout)

    def test_json_keeps_the_result_and_the_data_warning(self):
        admin, _calls = self.fake()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.AdminClient", admin):
                result = CliRunner().invoke(cli.app, ["server", "uninstall", "hetzner-vm", "--json"],
                                            env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["removed"]["wrappers"], ["shiplord-host", "shiplord-admin"])
        self.assertIn("Application data is never deleted", data["note"])

    def test_force_is_passed_to_the_server(self):
        admin, calls = self.fake()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.AdminClient", admin):
                result = CliRunner().invoke(cli.app, ["server", "uninstall", "hetzner-vm", "--force", "--json"],
                                            env=self.env(config))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[0][2], {"force": True})

    def test_a_refused_uninstall_exits_two_and_names_the_applications(self):
        error = ClientError("2 applications are still provisioned (orders, billing); deprovision them first "
                            "or pass --force", 2, error_code="rejected")
        admin, _calls = self.fake(error=error)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.AdminClient", admin):
                result = CliRunner().invoke(cli.app, ["server", "uninstall", "hetzner-vm", "--json"],
                                            env=self.env(config))
        self.assertEqual(result.exit_code, 2, result.output)
        data = json.loads(result.stdout)
        self.assertEqual(data["error_code"], "rejected")
        self.assertIn("orders", data["error"])

    def test_an_unknown_alias_exits_three_without_calling_the_server(self):
        admin, calls = self.fake()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            with patch("shiplord.cli.AdminClient", admin):
                result = CliRunner().invoke(cli.app, ["server", "uninstall", "absent", "--json"], env=self.env(config))
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertEqual(calls, [])
class EnvironmentDiffCLITests(unittest.TestCase):
    SECRET = "postgres://app:cli-secret@db/orders"
    MANIFEST = {"name": "orders", "server": "production"}

    def invoke(self, *arguments: str, input_text: str | None = None, current=("KEEP", "REMOVE")):
        calls = []

        def runner(command, payload, timeout):
            calls.append((command, payload))
            buffer = io.BytesIO()
            write_frame(buffer, {"status": "ok", "request_id": "x", "result": {"keys": list(current)}})
            return buffer.getvalue(), b"", 0

        with patch("shiplord.cli._manifest", return_value=self.MANIFEST), \
             patch("shiplord.cli._admin", return_value=AdminClient("admin@server.example.com", runner=runner)):
            result = CliRunner().invoke(cli.app, list(arguments), input=input_text)
        return result, calls

    def assert_key_only_requests(self, calls) -> None:
        for command, payload in calls:
            sent = read_frame(io.BytesIO(payload))
            self.assertEqual(sent["action"], "environment")
            self.assertEqual(sent["arguments"], {"operation": "keys"})
            self.assertNotIn(self.SECRET.encode(), payload)
            self.assertNotIn(self.SECRET, " ".join(command))

    def test_text_and_json_report_added_and_removed_names(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.env"
            candidate.write_text(f"KEEP={self.SECRET}\nADD={self.SECRET}-added\n")
            text, calls = self.invoke("env", "diff", "--from-file", str(candidate))
            data, json_calls = self.invoke("env", "diff", "--from-file", str(candidate), "--json")
        self.assertEqual(text.exit_code, 0, text.output)
        self.assertIn("Added: ADD", text.stdout)
        self.assertIn("Removed: REMOVE", text.stdout)
        self.assertIn("Key names only", text.stdout)
        self.assertNotIn("KEEP", text.stdout)
        self.assertEqual(json.loads(data.stdout)["added"], ["ADD"])
        self.assertEqual(json.loads(data.stdout)["removed"], ["REMOVE"])
        self.assertNotIn("KEEP", data.stdout)
        for result in (text, data):
            self.assertNotIn(self.SECRET, result.stdout + result.stderr)
        self.assert_key_only_requests(calls + json_calls)

    def test_stdin_is_supported_and_a_value_change_alone_is_not_a_change(self):
        result, calls = self.invoke("env", "diff", "--from-file", "-", input_text=f"KEEP={self.SECRET}\n",
                                    current=("KEEP",))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Added: none", result.stdout)
        self.assertIn("Removed: none", result.stdout)
        self.assertNotIn("KEEP", result.stdout)
        self.assertNotIn(self.SECRET, result.stdout + result.stderr)
        self.assertEqual(len(calls), 1)
        self.assert_key_only_requests(calls)

    def test_invalid_input_fails_before_the_remote_call(self):
        for payload, expected in (("PORT=1\n", "managed by Shiplord"), ("not a line\n", "not KEY=VALUE"),
                                  ("", "no variables")):
            with self.subTest(payload=payload):
                result, calls = self.invoke("env", "diff", "--from-file", "-", input_text=payload)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn(expected, result.stdout)
                self.assertEqual(calls, [])
        result, calls = self.invoke("env", "diff", "--from-file", "-", input_text=f"BAD LINE {self.SECRET}\n")
        self.assertEqual(result.exit_code, 2)
        self.assertNotIn(self.SECRET, result.stdout + result.stderr)
        self.assertEqual(calls, [])
        result, calls = self.invoke("env", "diff", "--from-file", "/does/not/exist.env")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("could not read the environment file", result.stdout)
        self.assertEqual(calls, [])
class CLIRecoveryTests(unittest.TestCase):
    """`recover` keeps manifest mode and adds a named target that works without a checkout."""

    OUTCOMES = (
        (0, {"ok": True, "status": "recovered", "exit_code": 0}),
        (0, {"ok": True, "status": "nothing_to_recover", "exit_code": 0}),
        (6, {"ok": False, "status": "intervention_required", "exit_code": 6, "error": "recovery_failed"}),
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config"
        self.project = root / "project"
        self.project.mkdir()
        self.write_servers(("hetzner-vm", "admin@host.example.com"))

    def write_servers(self, *servers: tuple[str, str]) -> None:
        directory = self.config / "shiplord" / "servers"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (self.config, directory.parent, directory):
            path.chmod(0o700)
        for alias, ssh in servers:
            record = directory / f"{alias}.json"
            record.write_text(json.dumps({"schema_version": 1, "alias": alias, "ssh": ssh}))
            record.chmod(0o600)

    def invoke(self, *arguments: str, cwd: Path | None = None):
        """Invoke from a directory with no ship.toml unless the test writes one."""
        env = {key: value for key, value in os.environ.items() if not key.startswith("SHIPLORD_")}
        env["XDG_CONFIG_HOME"] = str(self.config)
        current = Path.cwd()
        os.chdir(cwd or self.project)
        try:
            return CliRunner().invoke(cli.app, list(arguments), env=env)
        finally:
            os.chdir(current)

    def test_explicit_recovery_needs_no_manifest_and_uses_the_app_scoped_login(self):
        self.assertFalse((self.project / "ship.toml").exists())
        outcome = {"ok": True, "request_id": "rc-1", "status": "recovered", "exit_code": 0}
        with patch("shiplord.cli.commands.run_job_command", return_value=(0, outcome)) as run:
            result = self.invoke("recover", "orders", "--server", "hetzner-vm")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(run.call_args.args[0])  # no local manifest was invented for the wait bound
        client = run.call_args.args[1]
        self.assertEqual((client.application_id, client.target), ("orders", "ship-orders@host.example.com"))
        self.assertEqual(run.call_args.args[2], "recover")
        self.assertEqual(result.stderr, "")
        self.assertIn("Target: orders on hetzner-vm", result.stdout)
        self.assertIn("Outcome: recovered", result.stdout)

    def test_explicit_recovery_json_is_the_existing_result_shape(self):
        outcome = {"ok": True, "request_id": "rc-1", "status": "nothing_to_recover", "exit_code": 0}
        with patch("shiplord.cli.commands.run_job_command", return_value=(0, outcome)):
            result = self.invoke("recover", "orders", "--server", "hetzner-vm", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout), outcome)
        self.assertEqual(result.stderr, "")

    def test_explicit_recovery_requires_both_selectors_before_any_connection(self):
        for arguments in (("recover", "orders"), ("recover", "--server", "hetzner-vm")):
            with self.subTest(arguments=arguments), \
                    patch("shiplord.cli.commands.run_job_command") as run, \
                    patch("shiplord.cli.HelperClient") as helper:
                result = self.invoke(*arguments)
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn("both an application and a server alias", result.stdout)
            self.assertIn("shiplord recover <app> --server <alias>", result.stdout)
            run.assert_not_called()
            helper.assert_not_called()

    def test_explicit_recovery_rejects_invalid_selectors_before_any_connection(self):
        cases = ((("recover", "Orders", "--server", "hetzner-vm"), 2),
                 (("recover", "orders", "--server", "unknown"), 3))
        for arguments, code in cases:
            with self.subTest(arguments=arguments), \
                    patch("shiplord.cli.commands.run_job_command") as run, \
                    patch("shiplord.cli.HelperClient") as helper:
                result = self.invoke(*arguments, "--json")
            self.assertEqual(result.exit_code, code, result.output)
            self.assertFalse(json.loads(result.stdout)["ok"])
            run.assert_not_called()
            helper.assert_not_called()

    def test_recovery_outcomes_keep_their_exit_codes_in_explicit_mode(self):
        for code, outcome in self.OUTCOMES:
            with self.subTest(status=outcome["status"]), \
                    patch("shiplord.cli.commands.run_job_command", return_value=(code, outcome)):
                result = self.invoke("recover", "orders", "--server", "hetzner-vm")
            self.assertEqual(result.exit_code, code, result.output)
            self.assertEqual(result.stderr, "")
            self.assertIn(f"Outcome: {outcome['status'].replace('_', ' ')}", result.stdout)
            if outcome.get("error"):
                self.assertIn(f"Reason: {outcome['error'].replace('_', ' ')}", result.stdout)

    def test_recovery_outcomes_keep_their_exit_codes_in_manifest_mode(self):
        (self.project / "ship.toml").write_text(MANIFEST)
        for code, outcome in self.OUTCOMES:
            with self.subTest(status=outcome["status"]), \
                    patch("shiplord.cli._helper") as helper, \
                    patch("shiplord.cli.commands.run_job_command", return_value=(code, outcome)) as run:
                result = self.invoke("recover")
            self.assertEqual(result.exit_code, code, result.output)
            self.assertEqual(run.call_args.args[0]["name"], "orders")
            self.assertEqual(run.call_args.args[1], helper.return_value)
            self.assertIn(f"Outcome: {outcome['status'].replace('_', ' ')}", result.stdout)
            self.assertEqual(result.stderr, "")
class RecordingAdminClient:
    """A stand-in for the sudo client that records the resolved target and each request."""

    instances: list["RecordingAdminClient"] = []

    def __init__(self, target, prompt=None):
        self.target, self.prompt, self.calls = target, prompt, []
        RecordingAdminClient.instances.append(self)

    def call(self, action, application_id, arguments, **kwargs):
        self.calls.append((action, application_id, arguments))
        return {"removed": {"registration": True}}


class CLIDeprovisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config"
        self.write_servers(self.config, ("hetzner-vm", "admin@host.example.com"),
                           ("production", "admin@production.example.com"))
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.config))
        RecordingAdminClient.instances.clear()
        patcher = patch("shiplord.cli.AdminClient", RecordingAdminClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.directory = Path(self.temp.name) / "work"
        self.directory.mkdir()
        current = os.getcwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, current)

    def write_servers(self, config: Path, *servers: tuple[str, str]) -> None:
        directory = config / "shiplord" / "servers"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (config, directory.parent, directory):
            path.chmod(0o700)
        for alias, ssh in servers:
            path = directory / f"{alias}.json"
            path.write_text(json.dumps({"schema_version": 1, "alias": alias, "ssh": ssh}))
            path.chmod(0o600)

    def invoke(self, *args: str):
        return CliRunner().invoke(cli.app, list(args), env=self.env)

    def admin_calls(self) -> list[tuple]:
        return [call for instance in RecordingAdminClient.instances for call in instance.calls]

    def test_explicit_selectors_work_from_a_directory_without_a_manifest(self):
        result = self.invoke("deprovision", "orders", "--server", "hetzner-vm", "--yes", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.admin_calls(), [("deprovision", "orders", {})])
        self.assertEqual([instance.target for instance in RecordingAdminClient.instances],
                         ["admin@host.example.com"])
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_explicit_selectors_win_over_a_manifest_for_another_application(self):
        (self.directory / "ship.toml").write_text(MANIFEST.replace('name = "orders"', 'name = "billing"'))
        with patch("shiplord.cli.load_manifest", side_effect=AssertionError("explicit mode read ship.toml")):
            result = self.invoke("deprovision", "orders", "--server", "hetzner-vm", "--yes", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.admin_calls(), [("deprovision", "orders", {})])
        self.assertEqual(RecordingAdminClient.instances[0].target, "admin@host.example.com")

    def test_either_selector_alone_is_refused_before_any_request(self):
        for args in (("orders", "--yes"), ("--server", "hetzner-vm", "--yes")):
            with self.subTest(args=args):
                result = self.invoke("deprovision", *args, "--json")
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("both", json.loads(result.stdout)["error"])
                self.assertEqual(self.admin_calls(), [])

    def test_an_invalid_application_name_is_refused_before_any_request(self):
        for name in ("Orders", "orders_app", "a" * 49):
            with self.subTest(name=name):
                result = self.invoke("deprovision", name, "--server", "hetzner-vm", "--yes", "--json")
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("lowercase application name", json.loads(result.stdout)["error"])
                self.assertEqual(self.admin_calls(), [])

    def test_an_unknown_server_alias_is_refused_before_any_request(self):
        result = self.invoke("deprovision", "orders", "--server", "nowhere", "--yes", "--json")
        self.assertEqual(result.exit_code, 3, result.output)
        error = json.loads(result.stdout)["error"]
        self.assertIn("not registered", error)
        self.assertIn("hetzner-vm", error)
        self.assertEqual(self.admin_calls(), [])

    def test_missing_confirmation_names_the_resolved_target_and_sends_nothing(self):
        result = self.invoke("deprovision", "orders", "--server", "hetzner-vm", "--json")
        self.assertEqual(result.exit_code, 2, result.output)
        error = json.loads(result.stdout)["error"]
        for expected in ("orders", "hetzner-vm", "--yes"):
            self.assertIn(expected, error)
        self.assertEqual(self.admin_calls(), [])

    def test_manifest_mode_keeps_the_existing_request_and_json(self):
        (self.directory / "ship.toml").write_text(MANIFEST)
        for retire, arguments in ((False, {}), (True, {"retire": True})):
            with self.subTest(retire=retire):
                RecordingAdminClient.instances.clear()
                args = ["deprovision", "--yes", "--json"] + (["--retire"] if retire else [])
                result = self.invoke(*args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.admin_calls(), [("deprovision", "orders", arguments)])
                self.assertEqual(RecordingAdminClient.instances[0].target, "admin@production.example.com")
                output = json.loads(result.stdout)
                self.assertEqual(output["removed"], {"registration": True})
                self.assertIn("data", output["note"])

    def test_manifest_mode_requires_confirmation_and_names_the_target(self):
        (self.directory / "ship.toml").write_text(MANIFEST)
        result = self.invoke("deprovision", "--json")
        self.assertEqual(result.exit_code, 2, result.output)
        error = json.loads(result.stdout)["error"]
        for expected in ("orders", "production", "--yes"):
            self.assertIn(expected, error)
        self.assertEqual(self.admin_calls(), [])
