from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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

    def test_unimplemented_deploy_fails_without_server_access(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "deploy", "--image", "ghcr.io/example/orders@sha256:abc", "--json")
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not implemented", json.loads(result.stdout)["error"])

    def test_other_remote_commands_fail_without_writing_files(self):
        commands = [
            ["server", "add", "production", "--ssh", "appuser@example.com"],
            ["server", "bootstrap", "production"],
            ["init", "orders", "--template", "google-adk"],
            ["configure", "--server", "production", "--api-domain", "example.com"],
            ["provision"], ["status", "--json"], ["logs", "--tail", "10"],
            ["rollback", "--json"], ["recover", "--json"],
            ["github", "setup", "--repo", "QueryPlanner/orders"],
            ["github", "status", "--json"], ["github", "rotate-key"],
            ["doctor", "--server", "production", "--json"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            for command in commands:
                with self.subTest(command=command):
                    result = self.invoke(directory, *command)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("not implemented", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_doctor_rejects_conflicting_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "doctor", "--local", "--server", "production", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])

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
