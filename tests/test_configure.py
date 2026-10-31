from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord.cli import app
from shiplord.configure import ConfigurationError, collect_answers
from shiplord.manifest import load_manifest

NOTHING_GIVEN = {"server": None, "api_domain": None, "name": None, "runtime": None, "template": None,
                 "origins": None, "container_port": None, "readiness_path": None, "memory_mb": None}
SERVERS = [{"alias": "production", "ssh": "admin@203.0.113.7"}, {"alias": "staging", "ssh": "deploy@staging.example.com"}]


class Script:
    """Answers prompts in order; an empty answer takes the default, as pressing Enter does."""

    def __init__(self, *answers):
        self.answers, self.prompts, self.said = list(answers), [], []

    def ask(self, prompt, default):
        self.prompts.append((prompt, default))
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = self.answers.pop(0)
        return default if answer == "" and default is not None else answer

    def say(self, text):
        self.said.append(text)


class CollectAnswersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Handwriting_Studio"
        self.root.mkdir()

    def collect(self, script, given=None, servers=SERVERS):
        return collect_answers(dict(NOTHING_GIVEN, **(given or {})), root=self.root, servers=servers,
                               ask=script.ask, say=script.say)

    def test_existing_docker_repository_accepts_detected_defaults(self):
        (self.root / "Dockerfile").write_text("FROM node:22\nEXPOSE 3000\n")
        script = Script("2", "", "", "api.example.com", "", "", "")
        answers = self.collect(script)
        self.assertEqual(answers, {"server": "staging", "name": "handwriting-studio", "template": "generic",
                                   "runtime": "docker", "api_domain": "api.example.com", "origins": [],
                                   "container_port": 3000, "readiness_path": "/health", "memory_mb": 512})
        self.assertIn("Detected port 3000 from EXPOSE in Dockerfile.", script.said)
        self.assertNotIn("Frontend origins", " ".join(prompt for prompt, _ in script.prompts))  # generic: unused

    def test_only_missing_values_are_asked(self):
        script = Script("/health")
        answers = self.collect(script, {"server": "production", "api_domain": "api.example.com", "name": "orders",
                                        "template": "generic", "origins": [], "container_port": 8000,
                                        "memory_mb": 256})
        self.assertEqual([prompt for prompt, _ in script.prompts], ["Health check path"])
        self.assertEqual(answers["readiness_path"], "/health")

    def test_adk_asks_for_origins_and_defaults_to_ready(self):
        script = Script("production", "orders", "google-adk", "api.example.com",
                        "https://example.com, https://www.example.com", "", "", "")
        answers = self.collect(script)
        self.assertEqual(answers["origins"], ["https://example.com", "https://www.example.com"])
        self.assertEqual((answers["container_port"], answers["readiness_path"]), (8080, "/ready"))

    def test_invalid_answers_are_explained_and_asked_again(self):
        script = Script("9", "production", "Orders", "orders", "rails", "generic", "Api.Example.com", "203.0.113.7",
                        "api.example.com", "99999", "8000", "health", "/health", "8192", "1024")
        answers = self.collect(script)
        self.assertEqual((answers["server"], answers["name"], answers["template"], answers["api_domain"],
                          answers["container_port"], answers["readiness_path"], answers["memory_mb"]),
                         ("production", "orders", "generic", "api.example.com", 8000, "/health", 1024))
        self.assertTrue(any("from 16 to 4096" in line for line in script.said))

    def test_repeated_invalid_answers_stop_without_a_result(self):
        with self.assertRaisesRegex(ConfigurationError, "too many invalid answers"):
            self.collect(Script("nope", "still-nope", "0"))

    def test_without_registered_servers_the_user_is_sent_to_server_add(self):
        with self.assertRaisesRegex(ConfigurationError, "shiplord server add"):
            self.collect(Script(), servers=[])

    def test_a_single_server_is_the_default(self):
        script = Script("", "orders", "generic", "api.example.com", "", "", "")
        self.assertEqual(self.collect(script, servers=SERVERS[:1])["server"], "production")
        self.assertEqual(script.prompts[0], ("Server", "1"))

    def test_native_runtime_is_not_asked_for_a_container_port(self):
        script = Script("orders", "google-adk", "api.example.com", "", "", "")
        answers = self.collect(script, {"server": "production", "runtime": "native"})
        self.assertNotIn("Container port", [prompt for prompt, _ in script.prompts])
        self.assertEqual(answers["container_port"], 8080)

    def test_an_unusable_directory_name_is_not_offered_as_the_default(self):
        root = Path(self.temp.name) / "2024.site"
        root.mkdir()
        script = Script("orders", "generic", "api.example.com", "", "", "")
        collect_answers(dict(NOTHING_GIVEN, server="production"), root=root, servers=SERVERS, ask=script.ask,
                        say=script.say)
        self.assertEqual(script.prompts[0], ("Application name", None))


class InteractiveConfigureTests(unittest.TestCase):
    """The terminal path, run in-process with scripted keyboard input."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "orders"
        self.project.mkdir()
        (self.project / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n")
        servers = root / "config/shiplord/servers"
        servers.mkdir(parents=True)
        for path in (root / "config", servers.parent, servers):
            path.chmod(0o700)
        (servers / "production.json").write_text(json.dumps({"schema_version": 1, "alias": "production",
                                                             "ssh": "admin@example.com"}))
        (servers / "production.json").chmod(0o600)
        self.env = {"XDG_CONFIG_HOME": str(root / "config")}
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def run_configure(self, keyboard, *args):
        with patch.dict(os.environ, self.env), patch("shiplord.cli._interactive", return_value=True):
            return CliRunner().invoke(app, ["configure", *args], input=keyboard)

    def test_accepting_the_summary_writes_the_manifest(self):
        result = self.run_configure("\n\n\napi.example.com\n\n\n\ny\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("container port: 8000", result.output)
        self.assertIn("registered locally", result.output)
        config = load_manifest(self.project / "ship.toml")
        self.assertEqual((config["name"], config["server"], config["template"], config["http"]["container_port"]),
                         ("orders", "production", "generic", 8000))

    def test_declining_or_ending_input_writes_nothing(self):
        for keyboard in ("\n\n\napi.example.com\n\n\n\nn\n", "\n\n"):
            with self.subTest(keyboard=keyboard):
                result = self.run_configure(keyboard)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("no file was created", result.output)
                self.assertEqual(sorted(path.name for path in self.project.iterdir()), ["Dockerfile"])

    def test_an_existing_manifest_is_refused_before_any_question(self):
        (self.project / "ship.toml").write_text("keep")
        result = self.run_configure("")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertNotIn("Server", result.output)
        self.assertEqual((self.project / "ship.toml").read_text(), "keep")

    def test_flags_skip_their_questions(self):
        result = self.run_configure("api.example.com\n\n\ny\n", "--server", "production", "--name", "orders",
                                    "--template", "generic", "--container-port", "9000")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Servers:", result.output)
        self.assertEqual(load_manifest(self.project / "ship.toml")["http"]["container_port"], 9000)
