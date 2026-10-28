from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord.cli import app
from shiplord.template import MAX_DOWNLOAD, MAX_EXPANDED, MAX_MEMBERS, TEMPLATE_COMMIT, TemplateError, create_project

ROOT = f"google-adk-on-bare-metal-{TEMPLATE_COMMIT}"


def archive_bytes(extra=(), omit=()):
    files = {
        "pyproject.toml": b'[project]\nname = "google-adk-on-bare-metal"\n',
        "uv.lock": b'[[package]]\nname = "google-adk-on-bare-metal"\n',
        ".env.example": b"AGENT_NAME=local-agent\n",
        "tests/test_dependabot.py": b'assert package["name"] == "google-adk-on-bare-metal"\n',
        "tests/test_deployment_workflow.py": b'assert package["name"] == "google-adk-on-bare-metal"\n',
        "entrypoint.sh": b"#!/bin/sh\nexit 0\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo(ROOT)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content in files.items():
            if name in omit:
                continue
            item = tarfile.TarInfo(f"{ROOT}/{name}")
            item.mode = 0o755 if name.endswith(".sh") else 0o644
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        for item, content in extra:
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content) if item.isfile() else None)
    return buffer.getvalue()


class TemplateTests(unittest.TestCase):
    def test_init_creates_project_and_preserves_template_identity(self):
        directory_entry = tarfile.TarInfo(f"{ROOT}/empty-directory")
        directory_entry.type = tarfile.DIRTYPE
        with tempfile.TemporaryDirectory() as parent, patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes([(directory_entry, b"")]))):
            previous = os.getcwd()
            try:
                os.chdir(parent)
                result = CliRunner().invoke(app, ["init", "orders", "--template", "google-adk"])
            finally:
                os.chdir(previous)
            self.assertEqual(result.exit_code, 0, result.output)
            target = Path(parent) / "orders"
            self.assertIn('name = "orders"', (target / "pyproject.toml").read_text())
            self.assertIn('name = "orders"', (target / "uv.lock").read_text())
            self.assertEqual((target / ".env.example").read_text(), "AGENT_NAME=orders\n")
            self.assertIn('package["name"] == project["project"]["name"]', (target / "tests/test_deployment_workflow.py").read_text())
            self.assertEqual(json.loads((target / ".shiplord-template.json").read_text())["commit"], TEMPLATE_COMMIT)
            self.assertTrue((target / "entrypoint.sh").stat().st_mode & 0o111)
            self.assertTrue((target / "empty-directory").is_dir())

    def test_init_allows_an_existing_empty_directory(self):
        with tempfile.TemporaryDirectory() as parent, patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes())):
            (Path(parent) / "orders").mkdir()
            create_project(Path(parent), "orders", "google-adk")
            self.assertTrue((Path(parent) / "orders" / "pyproject.toml").is_file())

    def test_init_refuses_existing_content_and_symlink(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            (root / "orders").mkdir()
            (root / "orders" / "keep").write_text("keep")
            with self.assertRaises(TemplateError):
                create_project(root, "orders", "google-adk")
            self.assertEqual((root / "orders" / "keep").read_text(), "keep")
            (root / "linked").symlink_to(root / "orders", target_is_directory=True)
            with self.assertRaises(TemplateError):
                create_project(root, "linked", "google-adk")

    def test_init_rejects_unsafe_archives_before_creating_target(self):
        bad_items = []
        for name in (f"{ROOT}/../escape", f"{ROOT}/.git/config", "/absolute", f"{ROOT}/pyproject.toml"):
            item = tarfile.TarInfo(name)
            bad_items.append((item, b"bad"))
        link = tarfile.TarInfo(f"{ROOT}/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        bad_items.append((link, b""))
        for bad_item in bad_items:
            with self.subTest(path=bad_item[0].name), tempfile.TemporaryDirectory() as parent:
                with patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes([bad_item]))):
                    with self.assertRaises(TemplateError):
                        create_project(Path(parent), "orders", "google-adk")
                self.assertFalse((Path(parent) / "orders").exists())

    def test_init_offline_or_changed_template_leaves_no_target(self):
        with tempfile.TemporaryDirectory() as parent:
            with patch("urllib.request.urlopen", side_effect=OSError("offline")):
                with self.assertRaises(TemplateError):
                    create_project(Path(parent), "orders", "google-adk")
            with patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes(omit=("uv.lock",)))):
                with self.assertRaises(TemplateError):
                    create_project(Path(parent), "orders", "google-adk")
            self.assertFalse((Path(parent) / "orders").exists())

    def test_init_rejects_unknown_names_and_templates_at_the_cli(self):
        runner = CliRunner()
        invalid_name = runner.invoke(app, ["init", "../orders", "--template", "google-adk"])
        invalid_template = runner.invoke(app, ["init", "orders", "--template", "unknown"])
        self.assertEqual(invalid_name.exit_code, 2)
        self.assertEqual(invalid_template.exit_code, 2)

    def test_init_rejects_corrupt_archives_and_unexpected_template_contents(self):
        # A valid archive with a missing expected project name must not publish.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            root = tarfile.TarInfo(ROOT)
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            contents = b'[project]\nname = "different"\n'
            info = tarfile.TarInfo(f"{ROOT}/pyproject.toml")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
        for data in (b"not a tar archive", buffer.getvalue()):
            with self.subTest(data=data[:20]), tempfile.TemporaryDirectory() as parent:
                with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                    with self.assertRaises(TemplateError):
                        create_project(Path(parent), "orders", "google-adk")
                self.assertFalse((Path(parent) / "orders").exists())

    def test_init_rejects_missing_or_non_directory_archive_root(self):
        archives = []
        for is_file in (False, True):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                if is_file:
                    item = tarfile.TarInfo(ROOT)
                    item.size = 1
                    archive.addfile(item, io.BytesIO(b"x"))
            archives.append(buffer.getvalue())
        for data in archives:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as parent:
                with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                    with self.assertRaises(TemplateError):
                        create_project(Path(parent), "orders", "google-adk")
                self.assertFalse((Path(parent) / "orders").exists())

    def test_init_rejects_oversized_download_and_expansion(self):
        oversized = tarfile.TarInfo(f"{ROOT}/large")
        examples = [b"x" * (MAX_DOWNLOAD + 1), archive_bytes([(oversized, b"x" * (MAX_EXPANDED + 1))])]
        for data in examples:
            with self.subTest(length=len(data)), tempfile.TemporaryDirectory() as parent:
                with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                    with self.assertRaises(TemplateError):
                        create_project(Path(parent), "orders", "google-adk")
                self.assertFalse((Path(parent) / "orders").exists())

    def test_init_rejects_an_archive_with_too_many_entries(self):
        items = [(tarfile.TarInfo(f"{ROOT}/empty-{number}"), b"") for number in range(MAX_MEMBERS)]
        with tempfile.TemporaryDirectory() as parent, patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes(items))):
            with self.assertRaises(TemplateError):
                create_project(Path(parent), "orders", "google-adk")
            self.assertFalse((Path(parent) / "orders").exists())

    def test_init_rechecks_destination_after_downloading(self):
        with tempfile.TemporaryDirectory() as parent:
            target = Path(parent) / "orders"
            def download(*args, **kwargs):
                target.mkdir()
                (target / "keep").write_text("keep")
                return io.BytesIO(archive_bytes())
            with patch("urllib.request.urlopen", side_effect=download):
                with self.assertRaises(TemplateError):
                    create_project(Path(parent), "orders", "google-adk")
            self.assertEqual((target / "keep").read_text(), "keep")

    def test_init_reports_failure_to_create_target(self):
        with tempfile.TemporaryDirectory() as parent, patch("urllib.request.urlopen", return_value=io.BytesIO(archive_bytes())):
            with self.assertRaisesRegex(TemplateError, "could not publish"):
                create_project(Path(parent) / "missing", "orders", "google-adk")
