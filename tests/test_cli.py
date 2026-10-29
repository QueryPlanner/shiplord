from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from shiplord.configure import ConfigurationError, create_manifest
from shiplord.manifest import ManifestError, load_manifest

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
        return subprocess.run([str(executable), *args], cwd=directory, text=True, capture_output=True, check=False, env=env)

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
                    ["recover", "--json"], ["plan", "--json"], ["env", "keys", "--json"], ["doctor", "--json"],
                    ["deploy", "--image", "ghcr.io/example/orders@sha256:" + "a" * 64, "--json"]]
        with tempfile.TemporaryDirectory() as directory:
            for command in commands:
                with self.subTest(command=command):
                    result = self.invoke(directory, *command)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    if "--json" in command:
                        self.assertIn("ship.toml", json.loads(result.stdout)["error"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_deprovision_requires_explicit_confirmation_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            (Path(directory) / "ship.toml").write_text(MANIFEST)
            env = dict(os.environ, XDG_CONFIG_HOME=config)
            result = self.invoke(directory, "deprovision", "--retire", "--json", env=env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--yes", json.loads(result.stdout)["error"])

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
            for command in (["status", "--json"], ["provision", "--json"], ["rollback", "--json"]):
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

    def test_doctor_rejects_conflicting_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "doctor", "--local", "--server", "production", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])

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

    def test_configure_supports_an_explicit_empty_frontend(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com", "--no-frontend")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_manifest(Path(directory) / "ship.toml")["http"]["frontend_origins"], [])

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
        self.assertIn('"ssh": false', result.stdout)

    def test_version_and_help_use_shiplord(self):
        with tempfile.TemporaryDirectory() as directory:
            version = self.invoke(directory, "--version")
            help_result = self.invoke(directory, "--help")
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), "shiplord 0.1.0")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("shiplord", help_result.stdout.lower())
