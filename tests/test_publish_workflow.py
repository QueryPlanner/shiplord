"""Checks on .github/workflows/publish.yml, the PyPI Trusted Publishing workflow."""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def job(self, name):
        match = re.search(rf"^  {name}:\n(.*?)(?=^  \w|\Z)", self.text, re.S | re.M)
        self.assertIsNotNone(match, f"job {name} is missing")
        return match.group(1)

    def test_runs_on_published_release_or_manual_dispatch(self):
        on_block = re.search(r"^on:\n(.*?)^\S", self.text, re.S | re.M).group(1)
        self.assertIn("release:", on_block)
        self.assertIn("types: [published]", on_block)
        self.assertIn("workflow_dispatch:", on_block)
        self.assertNotIn("push:", on_block)
        self.assertNotIn("pull_request", on_block)

    def step_script(self, name):
        pattern = rf"^      - name: {re.escape(name)}\n.*?^        run: \|\n((?:^          [^\n]*\n|^\n)+)"
        match = re.search(pattern, self.text, re.M | re.S)
        self.assertIsNotNone(match, f"step {name} is missing")
        return textwrap.dedent(match.group(1))

    def test_manual_tag_must_be_stable_and_have_a_published_release(self):
        script = self.step_script("Validate manual release")
        with tempfile.TemporaryDirectory() as directory:
            fake_gh = Path(directory) / "gh"
            fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' \"$GH_RELEASE_RESULT\"\n")
            fake_gh.chmod(0o755)
            for tag, release, branch, succeeds in (
                ("v0.1.0", "true", "refs/heads/main", True),
                ("v0.1.0", "false", "refs/heads/main", False),
                ("v0.1.0; echo injected", "true", "refs/heads/main", False),
                ("main", "true", "refs/heads/main", False),
                ("v1.2.3/other", "true", "refs/heads/main", False),
                ("v0.1.0", "true", "refs/heads/MAIN", False),
            ):
                with self.subTest(tag=tag, release=release):
                    env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"],
                               RELEASE_TAG=tag, GH_RELEASE_RESULT=release,
                               GITHUB_REPOSITORY="QueryPlanner/shiplord", GITHUB_REF=branch)
                    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                            env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)

    def test_package_version_must_match_release_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "shiplord"
            package.mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
            runtime_version = package / "__init__.py"
            runtime_version.write_text('__version__ = "0.2.0"\n')
            result = self.verify_release(root, "v0.2.0")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.verify_release(root, "v0.3.0")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Release tag does not match the package version", result.stderr)
            runtime_version.write_text('__version__ = "0.1.0"\n')
            result = self.verify_release(root, "v0.2.0")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Package version does not match the CLI and helper version", result.stderr)

    def verify_release(self, root, tag):
        script = self.step_script("Verify package version")
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                              env=dict(os.environ, RELEASE_TAG=tag, PYTHONPATH="src", PYTHONDONTWRITEBYTECODE="1"),
                              capture_output=True, text=True)

    def test_release_requires_the_pinned_deploy_action_to_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(["git", "-c", "user.name=Release test",
                                       "-c", "user.email=release@example.test", "-c", "commit.gpgsign=false",
                                       *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            deploy = root / "deploy"
            deploy.mkdir()
            action = deploy / "action.yml"
            action.write_text("name: Reviewed action\n")
            git("add", "deploy")
            git("commit", "-qm", "Pin the reviewed action")
            pin = git("rev-parse", "HEAD")

            package = root / "src" / "shiplord"
            package.mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nversion = "0.3.0"\n')
            (package / "__init__.py").write_text('__version__ = "0.3.0"\n')
            workflow = package / "workflow.py"
            workflow.write_text(f"PYPI_DEPLOY_ACTION_REF = {pin!r}\n")
            result = self.verify_release(root, "v0.3.0")
            self.assertEqual(result.returncode, 0, result.stderr)

            action.write_text("name: Changed action\n")
            git("commit", "-qam", "Change the action without updating the pin")
            result = self.verify_release(root, "v0.3.0")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("The PyPI deploy action pin must match", result.stderr)

            workflow.write_text('PYPI_DEPLOY_ACTION_REF = "main"\n')
            result = self.verify_release(root, "v0.3.0")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("The PyPI deploy action must be pinned to a full commit SHA", result.stderr)

    def test_manual_dispatch_only_runs_from_main_and_checks_out_the_tag(self):
        build = self.job("build")
        self.assertIn("github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main'", build)
        self.assertIn("format('refs/tags/{0}', inputs.tag)", build)

    def test_only_the_publish_job_can_mint_an_oidc_token(self):
        self.assertEqual(self.text.count("id-token: write"), 1)
        self.assertIn("id-token: write", self.job("publish"))
        self.assertNotIn("id-token", self.job("build"))
        self.assertIn("contents: read", self.text.split("jobs:")[0])

    def test_publish_uses_the_pypi_environment_and_the_built_artifact(self):
        publish = self.job("publish")
        self.assertIn("environment: pypi", publish)
        self.assertIn("needs: build", publish)
        self.assertIn("download-artifact", publish)
        self.assertIn("gh-action-pypi-publish", publish)
        self.assertNotIn("password", publish)

    def test_every_external_action_is_pinned_to_a_full_commit(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertGreaterEqual(len(uses), 5)
        for ref in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$", ref)

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required")
    def test_passes_actionlint(self):
        result = subprocess.run(
            ["actionlint", "-shellcheck=", "-pyflakes=", str(WORKFLOW)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
