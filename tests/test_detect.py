from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from shiplord.detect import dockerfile, dockerfile_port, suggest_name, suggest_template


class DockerfilePortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def port(self, text, name="Dockerfile"):
        (self.root / name).write_text(text)
        return dockerfile_port(self.root)

    def test_simple_and_protocol_suffixed_ports(self):
        self.assertEqual(self.port("FROM node:22\nEXPOSE 3000\n"), 3000)
        self.assertEqual(self.port("FROM node:22\nexpose 3000/tcp\n"), 3000)
        self.assertIsNone(self.port("FROM node:22\nEXPOSE 53/udp\n"))

    def test_only_the_final_stage_counts(self):
        self.assertEqual(self.port("FROM node AS build\nEXPOSE 9229\nFROM nginx\nEXPOSE 80\n"), 80)
        self.assertIsNone(self.port("FROM node AS build\nEXPOSE 9229\nFROM nginx\n"))

    def test_first_port_of_the_last_expose_wins(self):
        self.assertEqual(self.port("FROM x\nEXPOSE 1000\nEXPOSE 8000 9000\n"), 8000)

    def test_arg_and_env_defaults_are_resolved(self):
        self.assertEqual(self.port("FROM x\nENV PORT=8000\nEXPOSE $PORT\n"), 8000)
        self.assertEqual(self.port("FROM x\nARG PORT=5000\nEXPOSE ${PORT}/tcp\n"), 5000)
        self.assertEqual(self.port("FROM x\nENV PORT 7000\nEXPOSE $PORT\n"), 7000)
        self.assertEqual(self.port("ARG PORT=4000\nFROM x\nARG PORT\nEXPOSE $PORT\n"), 4000)

    def test_unresolvable_or_invalid_ports_give_no_suggestion(self):
        for text in ("FROM x\nEXPOSE $PORT\n", "ARG PORT=4000\nFROM x\nEXPOSE $PORT\n",  # global ARG not redeclared
                     "FROM x\nEXPOSE 8000-8010\n", "FROM x\nEXPOSE 70000\n", "FROM x\nEXPOSE 0\n", "FROM x\n"):
            with self.subTest(text=text):
                self.assertIsNone(self.port(text))

    def test_comments_and_line_continuations(self):
        self.assertEqual(self.port("FROM x\n# EXPOSE 1\nEXPOSE \\\n  8080\n"), 8080)

    def test_containerfile_is_used_when_there_is_no_dockerfile(self):
        self.assertEqual(self.port("FROM x\nEXPOSE 8443\n", name="Containerfile"), 8443)

    def test_no_dockerfile_means_no_port_and_no_template(self):
        self.assertIsNone(dockerfile_port(self.root))
        self.assertIsNone(suggest_template(self.root))

    def test_a_dockerfile_suggests_the_generic_template(self):
        (self.root / "Dockerfile").write_text("FROM x\n")
        self.assertEqual(suggest_template(self.root), "generic")

    def test_untrusted_files_are_ignored_without_blocking(self):
        outside = self.root / "outside"
        outside.write_text("FROM x\nEXPOSE 3000\n")
        (self.root / "Dockerfile").symlink_to(outside)
        self.assertIsNone(dockerfile(self.root))
        (self.root / "Dockerfile").unlink()
        os.mkfifo(self.root / "Dockerfile")  # opening a FIFO without O_NONBLOCK would hang here
        self.assertIsNone(dockerfile_port(self.root))
        (self.root / "Dockerfile").unlink()
        (self.root / "Dockerfile").write_text("FROM x\nEXPOSE 3000\n" + "#" * 300_000)
        self.assertIsNone(dockerfile_port(self.root))
        (self.root / "Dockerfile").write_bytes(b"FROM x\nEXPOSE 3000\n\xff\xfe")
        self.assertIsNone(dockerfile_port(self.root))
        (self.root / "Dockerfile").unlink()
        (self.root / "Dockerfile").mkdir()
        self.assertIsNone(dockerfile_port(self.root))


class SuggestNameTests(unittest.TestCase):
    def test_directory_names_are_normalised_when_possible(self):
        self.assertEqual(suggest_name("handwriting-studio"), "handwriting-studio")
        self.assertEqual(suggest_name("My_App"), "my-app")
        self.assertEqual(suggest_name("orders api"), "orders-api")

    def test_names_that_cannot_be_application_names_give_no_suggestion(self):
        for name in ("123app", "app.v2", "", "a" * 49, "--"):
            with self.subTest(name=name):
                self.assertIsNone(suggest_name(name))
