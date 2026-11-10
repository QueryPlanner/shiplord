from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord import cli
from shiplord.client import ClientError
from shiplord.commands import CommandError
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

    def test_generic_is_an_explicit_docker_profile(self):
        contents = MANIFEST.replace('template = "google-adk"', 'template = "generic"')
        self.assertEqual(self.load(contents)["template"], "generic")
        with self.assertRaisesRegex(ManifestError, "generic template requires the docker runtime"):
            self.load(contents.replace('runtime = "docker"', 'runtime = "native"'))

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
        if env is None:  # configure consults the registered servers; never read the developer's own configuration
            env = dict(os.environ, XDG_CONFIG_HOME=str(Path(directory) / ".test-config"))
        # stdin is not a terminal, so no command may wait for an answer.
        return subprocess.run([str(executable), *args], cwd=directory, text=True, capture_output=True, check=False,
                              env=env, stdin=subprocess.DEVNULL)

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
                    ["recover", "--json"], ["restart", "--json"], ["plan", "--json"], ["env", "keys", "--json"],
                    ["doctor", "--json"], ["deploy", "--image", "ghcr.io/example/orders@sha256:" + "a" * 64, "--json"]]
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

    def test_generic_github_setup_rejects_before_saving_tailscale_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ship.toml").write_text(MANIFEST.replace('template = "google-adk"', 'template = "generic"'))
            config_home = root / "config"
            result = self.invoke(directory, "github", "setup", "--repo", "QueryPlanner/orders",
                                 "--tailscale-client-id", "throwaway-fixture", "--tailscale-audience",
                                 "api.tailscale.com/throwaway-fixture", "--json",
                                 env=dict(os.environ, XDG_CONFIG_HOME=str(config_home)))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("generic Docker applications need a custom workflow", json.loads(result.stdout)["error"])
            self.assertFalse(config_home.exists())
            self.assertEqual(list(root.iterdir()), [root / "ship.toml"])

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

    def test_version_and_help_use_shiplord(self):
        with tempfile.TemporaryDirectory() as directory:
            version = self.invoke(directory, "--version")
            help_result = self.invoke(directory, "--help")
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), "shiplord 0.2.0")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("shiplord", help_result.stdout.lower())


class CLIInventoryTests(unittest.TestCase):
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
        self.assertIn("shiplord recover", result.output)

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
        self.assertEqual(local_data["client"]["version"], "0.2.0")
        self.assertTrue(local_data["client"]["executable"])
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            self.write_servers(config, ("hetzner-vm", "admin@host.example.com"))
            with patch("shiplord.cli.probe_capabilities", return_value={"helper_version": "0.2.0", "actions": ["status"]}):
                server = CliRunner().invoke(cli.app, ["doctor", "--server", "hetzner-vm", "--json"], env=self.env(config))
        server_data = json.loads(server.stdout)
        self.assertEqual(server_data["helper_version"], "0.2.0")
        self.assertEqual(server_data["client"]["version"], "0.2.0")
