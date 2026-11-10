from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import unittest

from shiplord import __version__
from shiplord.manifest import validate_manifest
from shiplord.workflow import ACTIONS, WorkflowError, render_workflow

REF = "ab" * 20


def manifest(**changes):
    value = validate_manifest({"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk",
                               "runtime": "docker", "http": {"api_domain": "orders.example.com", "frontend_origins": [],
                               "container_port": 8080, "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
                               "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2},
                               "github": {"repository": "Example/orders", "deployment_branch": "main"}})
    value.update(changes)
    return value


def parse(text: str) -> dict:
    result = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load(STDIN.read))"], input=text,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("ruby"), "ruby is used as an independent YAML parser")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = render_workflow(manifest(), tooling_ref=REF)
        self.data = parse(self.text)
        self.jobs = self.data["jobs"]

    def steps(self, job):
        return self.jobs[job]["steps"]

    def test_the_document_is_valid_yaml_with_the_three_jobs_in_order(self):
        self.assertEqual(list(self.jobs), ["quality", "build", "deploy"])
        self.assertEqual((self.jobs["build"]["needs"], self.jobs["deploy"]["needs"]), ("quality", "build"))
        trigger = self.data.get("on") or self.data["true"]  # YAML 1.1 reads the key `on` as a boolean
        self.assertEqual(trigger["push"]["branches"], ["main"])
        self.assertIn("workflow_dispatch", trigger)
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("pull_request", trigger)  # pull requests run the template's own checks, never a deploy

    def test_every_external_action_is_pinned_to_a_full_commit(self):
        uses = re.findall(r"uses: (\S+)", self.text)
        external = [u for u in uses if not u.startswith("./")]
        self.assertTrue(external)
        for reference in external:
            self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$", reference)
        self.assertIn("./.github/workflows/code-quality.yml", uses)
        for line in ACTIONS.values():
            self.assertRegex(line.split(" # ")[0], r"@[0-9a-f]{40}$")

    def test_permissions_are_the_least_each_job_needs(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["quality"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.jobs["build"]["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(self.jobs["deploy"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.text.count("id-token: write"), 2)  # only where OIDC is used

    def test_a_production_deployment_is_serialized_and_never_cancelled(self):
        self.assertEqual(self.data["concurrency"], {"group": "shiplord-release-orders", "cancel-in-progress": False})
        for name in ("build", "deploy"):
            self.assertIn("timeout-minutes", self.jobs[name])
        self.assertEqual(self.jobs["deploy"]["if"], "${{ !cancelled() && github.ref == 'refs/heads/main' && "
                                                    "(inputs.check_only || needs.build.result == 'success') }}")

    def test_a_check_only_dispatch_skips_quality_and_build_and_runs_status_instead_of_deploy(self):
        trigger = self.data.get("on") or self.data["true"]
        self.assertEqual(trigger["workflow_dispatch"]["inputs"]["check_only"]["default"], False)
        self.assertEqual(self.jobs["quality"]["if"], "${{ !inputs.check_only }}")  # build needs quality: skipped too
        steps = {s.get("name"): s for s in self.steps("deploy")}
        self.assertEqual((steps["Check access only"]["if"], steps["Check access only"]["run"]),
                         ("${{ inputs.check_only }}", "shiplord status --json"))
        self.assertEqual(steps["Deploy"]["if"], "${{ !inputs.check_only }}")

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required to validate Actions expressions")
    def test_generated_workflows_pass_actionlint(self):
        for branch in ("main", "release/prod"):
            with self.subTest(branch=branch):
                value = manifest(github={"repository": "Example/orders", "deployment_branch": branch})
                result = subprocess.run(["actionlint", "-shellcheck=", "-pyflakes=", "-"],
                                        input=render_workflow(value, tooling_ref=REF),
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deployment_and_access_conditions_for_build_and_dispatch(self):
        # Model the generated boolean expressions for these specific GitHub contexts.
        def evaluate(condition, *, check_only, cancelled=False, ref="refs/heads/main", build="success"):
            expression = condition.removeprefix("${{").removesuffix("}}").strip()
            values = {"inputs.check_only": repr(check_only), "cancelled()": repr(cancelled),
                      "github.ref": repr(ref), "needs.build.result": repr(build)}
            for name, value in values.items():
                expression = expression.replace(name, value)
            expression = expression.replace("&&", " and ").replace("||", " or ")
            expression = re.sub(r"!(?!=)", " not ", expression).strip()
            tree = ast.parse(expression, mode="eval")
            allowed = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Constant,
                       ast.And, ast.Or, ast.Not, ast.Eq, ast.Load)
            self.assertTrue(all(isinstance(node, allowed) for node in ast.walk(tree)))
            return eval(compile(tree, "<workflow condition>", "eval"), {"__builtins__": {}})

        quality = self.jobs["quality"]["if"]
        deploy = self.jobs["deploy"]["if"]
        steps = {step.get("name"): step for step in self.steps("deploy")}
        for context, expected in (
            ({}, (True, True, False, True)),
            ({"build": "failure"}, (True, False, False, False)),
            ({"ref": "refs/heads/other"}, (True, False, False, False)),
            ({"cancelled": True}, (True, False, False, False)),
            ({"check_only": True, "build": "skipped"}, (False, True, True, False)),
        ):
            with self.subTest(context=context):
                context.setdefault("check_only", False)
                runs_quality = evaluate(quality, **context)
                runs_deploy = evaluate(deploy, **context)
                runs_check = runs_deploy and evaluate(steps["Check access only"]["if"], **context)
                runs_release = runs_deploy and evaluate(steps["Deploy"]["if"], **context)
                self.assertEqual((runs_quality, runs_deploy, runs_check, runs_release), expected)
        self.assertEqual(self.jobs["build"]["needs"], "quality")

    def test_no_secret_variable_or_event_value_is_interpolated_into_a_shell_script(self):
        for job in self.jobs.values():
            for step in job.get("steps", []):
                if "run" in step:
                    self.assertNotIn("${{", step["run"], step.get("name"))
        self.assertEqual(self.text.count("secrets.SHIPLORD_SSH_PRIVATE_KEY"), 1)
        key_step = next(s for s in self.steps("deploy") if "SSH_PRIVATE_KEY" in json.dumps(s.get("env", {})))
        self.assertEqual(key_step["env"]["SSH_PRIVATE_KEY"], "${{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}")
        self.assertIn("umask 077", key_step["run"])

    def test_the_deploy_step_uses_the_digest_the_exact_commit_a_retry_safe_id_and_requires_public_verification(self):
        deploy = next(s for s in self.steps("deploy") if s.get("name") == "Deploy")
        self.assertEqual(deploy["env"]["DIGEST"], "${{ needs.build.outputs.digest }}")
        for fragment in ('--image "${IMAGE}@${DIGEST}"', '--commit "$GITHUB_SHA"', '--request-id "gh-${RUN_ID}-${RUN_ATTEMPT}"',
                         "--require-public", "--json", "set -euo pipefail", "| tee"):
            self.assertIn(fragment, deploy["run"])

    def test_the_checkout_is_verified_and_credentials_are_not_persisted(self):
        checkouts = [s for job in self.jobs.values() for s in job.get("steps", []) if "actions/checkout" in s.get("uses", "")]
        self.assertTrue(checkouts)
        for step in checkouts:
            self.assertIs(step["with"]["persist-credentials"], False)
        self.assertTrue(any("git rev-parse HEAD" in s.get("run", "") for s in self.steps("deploy")))

    def test_the_tooling_is_installed_from_the_pinned_commit(self):
        install = next(s for s in self.steps("deploy") if s.get("name") == "Install the pinned Shiplord")
        self.assertIn(f"git+https://github.com/QueryPlanner/shiplord.git@{REF}", install["run"])

    def test_a_branch_reference_installs_from_git_at_that_branch(self):
        text = render_workflow(manifest(), tooling_ref="release/prod")
        install = next(s for s in parse(text)["jobs"]["deploy"]["steps"]
                       if s.get("name") == "Install the pinned Shiplord")
        self.assertEqual(install["run"],
                         "python -m pip install --disable-pip-version-check "
                         "\"git+https://github.com/QueryPlanner/shiplord.git@release/prod\"")

    def test_the_ssh_identity_is_removed_even_when_the_deploy_fails(self):
        cleanup = self.steps("deploy")[-1]
        self.assertEqual(cleanup["if"], "${{ always() }}")
        self.assertIn("shiplord_key", cleanup["run"])

    def test_the_tailscale_step_uses_oidc_inputs_and_a_default_tag(self):
        step = next(s for s in self.steps("deploy") if "tailscale/github-action" in s.get("uses", ""))
        self.assertEqual(step["with"]["oauth-client-id"], "${{ vars.SHIPLORD_TS_CLIENT_ID }}")
        self.assertEqual(step["with"]["audience"], "${{ vars.SHIPLORD_TS_AUDIENCE }}")
        self.assertEqual(step["with"]["ping"], "${{ vars.SHIPLORD_SERVER_HOST }}")  # waits until the server is reachable
        self.assertIn("tag:ci", step["with"]["tags"])
        self.assertNotIn("oauth-secret", self.text)  # federation: no long-lived Tailscale secret

    def test_the_image_build_targets_the_requested_platforms_and_records_the_commit(self):
        build = next(s for s in self.steps("build") if s.get("id") == "build-push")
        self.assertEqual(build["with"]["platforms"], "linux/amd64")
        self.assertIn("org.opencontainers.image.revision=${{ github.sha }}", build["with"]["labels"])
        self.assertFalse(any("qemu" in s.get("uses", "") for s in self.steps("build")))
        both = parse(render_workflow(manifest(), tooling_ref=REF, platforms="linux/amd64,linux/arm64"))
        self.assertTrue(any("qemu" in s.get("uses", "") for s in both["jobs"]["build"]["steps"]))

    def test_output_is_deterministic_and_follows_the_deployment_branch(self):
        self.assertEqual(self.text, render_workflow(manifest(), tooling_ref=REF))
        other = manifest(github={"repository": "Example/orders", "deployment_branch": "release/prod"})
        data = parse(render_workflow(other, tooling_ref=REF))
        self.assertEqual((data.get("on") or data["true"])["push"]["branches"], ["release/prod"])
        self.assertIn("github.ref == 'refs/heads/release/prod'", data["jobs"]["deploy"]["if"])

    def test_invalid_inputs_are_refused(self):
        for kwargs in ({"tooling_ref": "bad ref"}, {"tooling_ref": "main\n"}, {"tooling_ref": "release/../main"},
                       {"tooling_ref": REF, "platforms": "linux/386"},
                       {"tooling_ref": REF, "platforms": "windows/amd64"},
                       {"tooling_ref": REF, "tooling_repository": "evil repo; rm"},
                       {"tooling_version": "0.2.0; rm -rf /"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WorkflowError):
                render_workflow(manifest(), **kwargs)
        with self.assertRaisesRegex(WorkflowError, "native"):
            render_workflow(manifest(runtime="native"), tooling_ref=REF)


class ToolingInstallTests(unittest.TestCase):
    """The install line needs no YAML parser: a released CLI must pin itself from PyPI by default."""

    def install_run(self, **kwargs) -> str:
        text = render_workflow(manifest(), **kwargs)
        return next(line for line in text.splitlines() if "pip install" in line).strip()

    def test_the_default_install_pins_the_running_version_from_pypi_not_a_commit(self):
        run = self.install_run()
        self.assertEqual(run, f"run: python -m pip install --disable-pip-version-check \"shiplord=={__version__}\"")
        self.assertNotIn("git+", run)
        self.assertNotIn("@", run)  # a version requirement, never a git reference or commit

    def test_a_git_reference_is_still_available_for_unreleased_code(self):
        self.assertIn(f"git+https://github.com/QueryPlanner/shiplord.git@{REF}", self.install_run(tooling_ref=REF))
        self.assertIn("git+https://github.com/QueryPlanner/shiplord.git@main", self.install_run(tooling_ref="main"))


if __name__ == "__main__":
    unittest.main()
