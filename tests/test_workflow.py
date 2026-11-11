from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from shiplord.manifest import validate_manifest
from shiplord.workflow import (ACTIONS, DEFAULT_QUALITY_WORKFLOW, DEPLOY_WORKFLOW_PATH, PINS, WORKFLOW_PATH, PinChange,
                               WorkflowError, check_action_pins, is_generated_workflow, parse_action_pin,
                               render_deploy_workflow, render_workflow, upgrade_action_pins)

REF = "ab" * 20
QUALITY = "./" + DEFAULT_QUALITY_WORKFLOW
DEPLOY = "./" + DEPLOY_WORKFLOW_PATH
ACTION_PATH = Path(__file__).resolve().parents[1] / "deploy" / "action.yml"

BASE_MANIFEST = {"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk",
                 "runtime": "docker", "http": {"api_domain": "orders.example.com", "frontend_origins": [],
                 "container_port": 8080, "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
                 "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}


def manifest(runtime="docker", **changes):
    value = validate_manifest({**BASE_MANIFEST, "runtime": runtime,
                               "github": {"repository": "Example/orders", "deployment_branch": "main"}})
    value.update(changes)
    return value


def native_manifest(**changes):
    value = manifest("native")
    value["github"] = {"repository": "Example/orders", "deployment_branch": "main",
                       "build_command": "go build -trimpath -o dist/orders ./cmd/orders",
                       "binary_path": "dist/orders"}
    value.update(changes)
    return value


def manifest_without_branch():
    return validate_manifest({**BASE_MANIFEST, "github": {"repository": "Example/orders"}})


def parse(text: str) -> dict:
    result = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load(STDIN.read))"], input=text,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def declared_inputs(path: Path) -> dict[str, dict[str, str]]:
    """The flat ``inputs:`` block of a composite action, read without a YAML library.

    The repository has no YAML dependency, so this reads exactly the two-level shape the action uses and
    refuses anything else instead of guessing at a structure it does not understand.
    """
    declared: dict[str, dict[str, str]] = {}
    inside, current = False, None
    for number, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            inside, current = stripped == "inputs:", None
        elif not inside:
            continue
        elif indent == 2 and stripped.endswith(":"):
            current = stripped[:-1]
            declared[current] = {}
        elif indent == 4 and current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            declared[current][key.strip()] = value.strip().strip("\"'")
        else:
            raise AssertionError(f"{path.name}:{number} is not a flat input declaration")
    return declared


def passed_inputs(text: str, reference: str) -> dict[str, str]:
    """The ``with:`` values a generated workflow passes to one action, read without a YAML library."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if line.strip().split(" # ")[0] == f"uses: {reference}"), None)
    if start is None:
        raise AssertionError(f"the generated workflow does not call {reference}")
    values: dict[str, str] = {}
    inside = False
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < 8 or (indent == 8 and line.strip() != "with:"):
            break
        if indent == 8:
            inside = True
            continue
        key, separator, value = line.strip().partition(":")
        if not inside or indent != 10 or not separator:
            raise AssertionError(f"unexpected line in the action step: {line!r}")
        values[key] = value.strip()
    return values


@unittest.skipUnless(shutil.which("ruby"), "ruby is used as an independent YAML parser")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = render_workflow(manifest(), tooling_ref=REF, quality_workflow=DEFAULT_QUALITY_WORKFLOW)
        self.data = parse(self.text)
        self.jobs = self.data["jobs"]
        self.deploy_text = render_deploy_workflow(manifest(), tooling_ref=REF)
        self.deploy = parse(self.deploy_text)
        self.deploy_jobs = self.deploy["jobs"]
        self.action_text = ACTION_PATH.read_text()
        self.action = parse(self.action_text)
        self.action_steps = self.action["runs"]["steps"]

    def steps(self, job, document=None):
        return (document or self.jobs)[job]["steps"]

    def test_the_pipeline_is_a_trigger_that_calls_a_reusable_deploy_workflow(self):
        self.assertEqual(list(self.jobs), ["quality", "deploy"])
        self.assertEqual(self.jobs["quality"]["uses"], QUALITY)
        self.assertEqual((self.jobs["deploy"]["uses"], self.jobs["deploy"]["needs"]), (DEPLOY, "quality"))
        self.assertEqual(self.jobs["deploy"]["with"], {"check_only": "${{ inputs.check_only }}"})
        self.assertEqual(self.jobs["deploy"]["secrets"],
                         {"SHIPLORD_SSH_PRIVATE_KEY": "${{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}"})
        trigger = self.deploy.get("on") or self.deploy["true"]
        self.assertEqual(list(trigger), ["workflow_call"])
        self.assertEqual(trigger["workflow_call"]["secrets"], {"SHIPLORD_SSH_PRIVATE_KEY": {"required": True}})
        self.assertEqual(list(self.deploy_jobs), ["build", "deploy"])
        self.assertEqual(self.deploy_jobs["deploy"]["needs"], "build")
        for document in (self.data, self.deploy):
            trigger = document.get("on") or document["true"]
            self.assertNotIn("pull_request_target", trigger)
            self.assertNotIn("pull_request", trigger)  # pull requests run the template's own checks, never a deploy

    def test_the_trigger_still_runs_on_the_deployment_branch_and_on_dispatch(self):
        trigger = self.data.get("on") or self.data["true"]
        self.assertEqual(trigger["push"]["branches"], ["main"])
        self.assertEqual(trigger["workflow_dispatch"]["inputs"]["check_only"]["default"], False)

    def test_a_pipeline_without_a_quality_workflow_has_no_quality_job_and_no_broken_reference(self):
        text = render_workflow(manifest(), tooling_ref=REF)
        data = parse(text)
        self.assertEqual(list(data["jobs"]), ["deploy"])
        self.assertNotIn("quality", text)
        self.assertNotIn("code-quality", text)
        self.assertEqual(data["jobs"]["deploy"]["uses"], DEPLOY)
        self.assertNotIn("needs", data["jobs"]["deploy"])
        self.assertEqual(data["jobs"]["deploy"]["if"], "${{ !cancelled() }}")

    def test_every_local_uses_names_a_generated_file_or_the_named_quality_workflow(self):
        self.assertEqual([u for u in re.findall(r"uses: (\S+)", self.text) if u.startswith("./")], [QUALITY, DEPLOY])
        self.assertEqual([u for u in re.findall(r"uses: (\S+)", self.deploy_text) if u.startswith("./")], [])

    def test_every_external_action_is_pinned_to_a_full_commit(self):
        for text in (self.deploy_text, self.action_text):
            external = [u for u in re.findall(r"uses: (\S+)", text) if not u.startswith("./")]
            self.assertTrue(external, text.splitlines()[0])
            for reference in external:
                self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$", reference)
        for line in ACTIONS.values():
            self.assertRegex(line.split(" # ")[0], r"@[0-9a-f]{40}$")
        # The action file names each pin's release, and its tailscale pin must not drift from ACTIONS.
        for line in self.action_text.splitlines():
            if line.strip().startswith("uses:"):
                self.assertRegex(line.strip(), r"^uses: \S+@[0-9a-f]{40} # v\S+$")
        tailscale = next(u for u in re.findall(r"uses: (\S+)", self.action_text) if "tailscale" in u)
        self.assertEqual(tailscale, ACTIONS["tailscale"].split(" # ")[0])

    def test_permissions_are_the_least_each_job_needs(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["quality"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.jobs["deploy"]["permissions"],
                         {"contents": "read", "id-token": "write", "packages": "write"})  # caps the called jobs
        self.assertEqual(self.deploy["permissions"], {"contents": "read"})
        self.assertEqual(self.deploy_jobs["build"]["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(self.deploy_jobs["deploy"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.text.count("id-token: write"), 2)
        self.assertEqual(self.deploy_text.count("id-token: write"), 1)  # only where OIDC is used

    def test_the_deploy_job_hands_the_action_every_value_the_inlined_steps_used(self):
        steps = self.steps("deploy", self.deploy_jobs)
        action = steps[-1]
        self.assertEqual((action["name"], action["uses"]),
                         ("Deploy with Shiplord", f"QueryPlanner/shiplord/deploy@{REF}"))
        self.assertIn(f"uses: QueryPlanner/shiplord/deploy@{REF}", self.deploy_text)
        self.assertEqual(action["with"], {
            "check-only": "${{ inputs.check_only }}",
            "ssh-private-key": "${{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}",
            "ssh-known-hosts": "${{ vars.SHIPLORD_SSH_KNOWN_HOSTS }}",
            "deploy-user": "${{ vars.SHIPLORD_DEPLOY_USER }}",
            "server-host": "${{ vars.SHIPLORD_SERVER_HOST }}",
            "ssh-port": "${{ vars.SHIPLORD_SSH_PORT }}",
            "ts-client-id": "${{ vars.SHIPLORD_TS_CLIENT_ID }}",
            "ts-audience": "${{ vars.SHIPLORD_TS_AUDIENCE }}",
            "ts-tags": "${{ vars.SHIPLORD_TS_TAGS || 'tag:ci' }}",
            "image": "${{ needs.build.outputs.image }}",
            "digest": "${{ needs.build.outputs.digest }}",
            "commit": "${{ github.sha }}",
            "request-id": "gh-${{ github.run_id }}-${{ github.run_attempt }}",
        })
        # The tailnet join, the SSH identity, the deploy and the summary live in the action now, not here.
        for inlined in ("tailscale/github-action", "shiplord deploy", "shiplord status", "SHIPLORD_SSH_IDENTITY",
                        "GITHUB_STEP_SUMMARY"):
            self.assertNotIn(inlined, self.deploy_text)
        self.assertEqual([s.get("name") for s in steps if s.get("name")],
                         ["Verify the checked-out commit", "Install the pinned Shiplord", "Deploy with Shiplord"])

    def test_a_production_deployment_is_serialized_and_never_cancelled(self):
        self.assertEqual(self.data["concurrency"], {"group": "shiplord-release-orders", "cancel-in-progress": False})
        for name in ("build", "deploy"):
            self.assertIn("timeout-minutes", self.deploy_jobs[name])
        self.assertEqual(self.deploy_jobs["deploy"]["if"],
                         "${{ !cancelled() && github.ref == 'refs/heads/main' && "
                         "(inputs.check_only || needs.build.result == 'success') }}")

    def test_a_check_only_dispatch_skips_quality_and_build_and_runs_status_instead_of_deploy(self):
        self.assertEqual(self.jobs["quality"]["if"], "${{ !inputs.check_only }}")
        self.assertEqual(self.jobs["deploy"]["if"],
                         "${{ !cancelled() && (inputs.check_only || needs.quality.result == 'success') }}")
        self.assertEqual(self.deploy_jobs["build"]["if"], "${{ !inputs.check_only }}")
        steps = {s.get("name"): s for s in self.action_steps}
        # A composite action has no boolean inputs, so the workflow passes "true"/"false" and the action compares.
        self.assertEqual(steps["Check access only"]["if"], "${{ inputs.check-only == 'true' }}")
        self.assertEqual((steps["Check access only"]["run"], steps["Check access only"]["shell"]),
                         ("shiplord status --json", "bash"))
        self.assertEqual(steps["Deploy"]["if"], "${{ inputs.check-only != 'true' }}")
        self.assertEqual(self.steps("deploy", self.deploy_jobs)[-1]["with"]["check-only"], "${{ inputs.check_only }}")

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required to validate Actions expressions")
    def test_generated_workflows_pass_actionlint(self):
        for branch in ("main", "release/prod"):
            value = manifest(github={"repository": "Example/orders", "deployment_branch": branch})
            for quality in (None, DEFAULT_QUALITY_WORKFLOW):
                for pin in ({"tooling_ref": REF}, {"tooling_version": "0.2.0"}):
                    for text in (render_workflow(value, **pin, quality_workflow=quality),
                                 render_deploy_workflow(value, **pin)):
                        with self.subTest(branch=branch, quality=quality, pin=pin, text=text.splitlines()[0]):
                            result = subprocess.run(["actionlint", "-shellcheck=", "-pyflakes=", "-"],
                                                    input=text, capture_output=True, text=True, timeout=30)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for pin in ({"tooling_ref": REF}, {"tooling_version": "0.2.0"}):
            native = render_workflow(native_manifest(), **pin)
            with self.subTest(pin=pin):
                result = subprocess.run(["actionlint", "-shellcheck=", "-pyflakes=", "-"], input=native,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required to validate Actions expressions")
    def test_an_upgraded_workflow_is_still_valid_yaml_and_passes_actionlint(self):
        for branch in ("main", "release/prod"):
            value = manifest(github={"repository": "Example/orders", "deployment_branch": branch})
            for quality in (None, DEFAULT_QUALITY_WORKFLOW):
                for text in (render_workflow(value, tooling_ref=REF, quality_workflow=quality),
                             render_deploy_workflow(value, tooling_ref=REF)):
                    stale = text.replace(PINS["checkout"].commit, "1" * 40)
                    upgraded, _changes = upgrade_action_pins(stale)
                    with self.subTest(branch=branch, quality=quality, text=text.splitlines()[0]):
                        self.assertEqual(upgrade_action_pins(upgraded)[1], [])  # nothing left to refresh
                        self.assertNotIn("1" * 40, upgraded)  # the stale commit is gone
                        parse(upgraded)  # ruby's YAML parser accepts the refreshed file
                        result = subprocess.run(["actionlint", "-shellcheck=", "-pyflakes=", "-"],
                                                input=upgraded, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deployment_and_access_conditions_for_build_and_dispatch(self):
        # Model the generated boolean expressions for these specific GitHub contexts.
        def evaluate(condition, *, check_only, cancelled=False, ref="refs/heads/main", build="success",
                     quality="success"):
            expression = condition.removeprefix("${{").removesuffix("}}").strip()
            values = {"inputs.check_only": repr(check_only), "inputs.check-only": repr(str(check_only).lower()),
                      "cancelled()": repr(cancelled), "github.ref": repr(ref),
                      "needs.build.result": repr(build), "needs.quality.result": repr(quality)}
            for name, value in values.items():
                expression = expression.replace(name, value)
            expression = expression.replace("&&", " and ").replace("||", " or ")
            expression = re.sub(r"!(?!=)", " not ", expression).strip()
            tree = ast.parse(expression, mode="eval")
            allowed = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Constant,
                       ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Load)
            self.assertTrue(all(isinstance(node, allowed) for node in ast.walk(tree)))
            return eval(compile(tree, "<workflow condition>", "eval"), {"__builtins__": {}})

        quality_if = self.jobs["quality"]["if"]
        caller_if = self.jobs["deploy"]["if"]
        build_if = self.deploy_jobs["build"]["if"]
        deploy_if = self.deploy_jobs["deploy"]["if"]
        steps = {step.get("name"): step for step in self.action_steps}
        for context, expected in (
            ({}, (True, True, True, True, False, True)),
            ({"build": "failure"}, (True, True, True, False, False, False)),
            ({"quality": "failure"}, (True, False, False, False, False, False)),
            ({"ref": "refs/heads/other"}, (True, True, True, False, False, False)),
            ({"cancelled": True}, (True, False, False, False, False, False)),
            ({"check_only": True, "build": "skipped", "quality": "skipped"}, (False, True, False, True, True, False)),
        ):
            with self.subTest(context=context):
                context.setdefault("check_only", False)
                runs_quality = evaluate(quality_if, **context)
                runs_caller = evaluate(caller_if, **context)
                runs_build = runs_caller and evaluate(build_if, **context)
                runs_deploy = runs_caller and evaluate(deploy_if, **context)
                runs_check = runs_deploy and evaluate(steps["Check access only"]["if"], **context)
                runs_release = runs_deploy and evaluate(steps["Deploy"]["if"], **context)
                self.assertEqual((runs_quality, runs_caller, runs_build, runs_deploy, runs_check, runs_release),
                                 expected)

    def test_no_secret_variable_or_event_value_is_interpolated_into_a_shell_script(self):
        step_lists = [job.get("steps", []) for document in (self.data, self.deploy)
                      for job in document["jobs"].values()]
        for steps in step_lists + [self.action_steps]:
            for step in steps:
                if "run" in step:
                    self.assertNotIn("${{", step["run"], step.get("name"))
        self.assertEqual(self.deploy_text.count("secrets.SHIPLORD_SSH_PRIVATE_KEY"), 1)
        self.assertEqual(self.action_text.count("inputs.ssh-private-key"), 1)  # the secret is mapped once, to env
        key_step = next(s for s in self.action_steps if "SSH_PRIVATE_KEY" in json.dumps(s.get("env", {})))
        self.assertEqual(key_step["env"]["SSH_PRIVATE_KEY"], "${{ inputs.ssh-private-key }}")
        self.assertIn("umask 077", key_step["run"])
        self.assertEqual(self.text.count("secrets.SHIPLORD_SSH_PRIVATE_KEY"), 1)  # only the explicit mapping
        # Nothing the action does can hand a secret back to the caller or the run page.
        self.assertNotIn("\noutputs:", self.action_text)
        summary = next(s for s in self.action_steps if s.get("name") == "Summary")
        self.assertNotIn("SSH", summary["run"])

    def test_the_deploy_step_uses_the_digest_the_exact_commit_a_retry_safe_id_and_requires_public_verification(self):
        deploy = next(s for s in self.action_steps if s.get("name") == "Deploy")
        self.assertEqual(deploy["env"], {"IMAGE": "${{ inputs.image }}", "DIGEST": "${{ inputs.digest }}",
                                         "COMMIT": "${{ inputs.commit }}", "REQUEST_ID": "${{ inputs.request-id }}"})
        for fragment in ('--image "${IMAGE}@${DIGEST}"', '--commit "$COMMIT"', '--request-id "$REQUEST_ID"',
                         "--require-public", "--json", "set -euo pipefail", "| tee"):
            self.assertIn(fragment, deploy["run"])
        passed = self.steps("deploy", self.deploy_jobs)[-1]["with"]
        self.assertEqual((passed["commit"], passed["request-id"]),
                         ("${{ github.sha }}", "gh-${{ github.run_id }}-${{ github.run_attempt }}"))

    def test_the_checkout_is_verified_and_credentials_are_not_persisted(self):
        checkouts = [s for job in self.deploy_jobs.values() for s in job.get("steps", [])
                     if "actions/checkout" in s.get("uses", "")]
        self.assertTrue(checkouts)
        for step in checkouts:
            self.assertIs(step["with"]["persist-credentials"], False)
        self.assertTrue(any("git rev-parse HEAD" in s.get("run", "") for s in self.steps("deploy", self.deploy_jobs)))

    def test_the_tooling_is_installed_from_the_pinned_commit(self):
        install = next(s for s in self.steps("deploy", self.deploy_jobs) if s.get("name") == "Install the pinned Shiplord")
        self.assertIn(f"git+https://github.com/QueryPlanner/shiplord.git@{REF}", install["run"])

    def test_the_installed_version_can_pin_the_deploy_workflow(self):
        data = parse(render_deploy_workflow(manifest(), tooling_version="0.2.0"))
        install = next(s for s in data["jobs"]["deploy"]["steps"] if s.get("name") == "Install the pinned Shiplord")
        self.assertIn("shiplord==0.2.0", install["run"])
        self.assertEqual(data["jobs"]["deploy"]["steps"][-1]["uses"],
                         "QueryPlanner/shiplord/deploy@465a02fd0d76308546893d2cc37a94254e07c598")
        self.assertNotIn("@None", render_deploy_workflow(manifest(), tooling_version="0.2.0"))
        with self.assertRaises(WorkflowError):
            render_deploy_workflow(manifest())
        with self.assertRaises(WorkflowError):
            render_deploy_workflow(manifest(), tooling_ref=REF, tooling_version="0.2.0")

    def test_the_ssh_identity_is_removed_even_when_the_deploy_fails(self):
        self.assertEqual(self.steps("deploy", self.deploy_jobs)[-1]["name"], "Deploy with Shiplord")
        cleanup = self.action_steps[-1]
        self.assertEqual(cleanup["name"], "Remove the SSH identity")
        self.assertEqual(cleanup["if"], "${{ always() }}")
        self.assertIn("shiplord_key", cleanup["run"])

    def test_the_tailscale_step_uses_oidc_inputs_and_a_default_tag(self):
        step = next(s for s in self.action_steps if "tailscale/github-action" in s.get("uses", ""))
        self.assertEqual(step["with"]["oauth-client-id"], "${{ inputs.ts-client-id }}")
        self.assertEqual(step["with"]["audience"], "${{ inputs.ts-audience }}")
        self.assertEqual(step["with"]["ping"], "${{ inputs.server-host }}")  # waits until the server is reachable
        self.assertEqual(step["with"]["tags"], "${{ inputs.ts-tags }}")
        self.assertEqual(self.action["inputs"]["ts-tags"]["default"], "tag:ci")
        passed = self.steps("deploy", self.deploy_jobs)[-1]["with"]
        self.assertEqual(passed["ts-tags"], "${{ vars.SHIPLORD_TS_TAGS || 'tag:ci' }}")
        self.assertNotIn("oauth-secret", self.text + self.deploy_text + self.action_text)  # no long-lived TS secret

    def test_the_action_is_composite_and_its_scripts_declare_a_shell(self):
        self.assertEqual((self.action["name"], self.action["runs"]["using"]), ("Shiplord deploy", "composite"))
        self.assertTrue(self.action["description"])
        for step in self.action_steps:
            if "run" in step:
                self.assertEqual(step["shell"], "bash", step.get("name"))
            else:
                self.assertIn("uses", step)

    def test_the_action_writes_the_summary_from_the_deploy_result(self):
        summary = next(s for s in self.action_steps if s.get("name") == "Summary")
        self.assertEqual(summary["if"], "${{ always() }}")
        for fragment in ("shiplord-result.json", "GITHUB_STEP_SUMMARY", "status", "commit", "public"):
            self.assertIn(fragment, summary["run"])

    def test_the_image_build_targets_the_requested_platforms_and_records_the_commit(self):
        build = next(s for s in self.steps("build", self.deploy_jobs) if s.get("id") == "build-push")
        self.assertEqual(build["with"]["platforms"], "linux/amd64")
        self.assertIn("org.opencontainers.image.revision=${{ github.sha }}", build["with"]["labels"])
        self.assertFalse(any("qemu" in s.get("uses", "") for s in self.steps("build", self.deploy_jobs)))
        both = parse(render_deploy_workflow(manifest(), tooling_ref=REF, platforms="linux/amd64,linux/arm64"))
        self.assertTrue(any("qemu" in s.get("uses", "") for s in both["jobs"]["build"]["steps"]))

    def test_output_is_deterministic_and_follows_the_deployment_branch(self):
        self.assertEqual(self.text, render_workflow(manifest(), tooling_ref=REF, quality_workflow=DEFAULT_QUALITY_WORKFLOW))
        self.assertEqual(self.deploy_text, render_deploy_workflow(manifest(), tooling_ref=REF))
        other = manifest(github={"repository": "Example/orders", "deployment_branch": "release/prod"})
        data = parse(render_workflow(other, tooling_ref=REF))
        self.assertEqual((data.get("on") or data["true"])["push"]["branches"], ["release/prod"])
        deploy = parse(render_deploy_workflow(other, tooling_ref=REF))
        self.assertIn("github.ref == 'refs/heads/release/prod'", deploy["jobs"]["deploy"]["if"])

    def test_a_workflow_generated_for_trunk_triggers_and_deploys_from_trunk(self):
        trunk = manifest(github={"repository": "Example/orders", "deployment_branch": "trunk"})
        text = render_workflow(trunk, tooling_ref=REF)
        data = parse(text)
        self.assertEqual((data.get("on") or data["true"])["push"]["branches"], ["trunk"])
        self.assertNotIn("refs/heads/main", text)

    def test_the_deployment_branch_defaults_to_main_when_the_manifest_omits_it(self):
        value = manifest_without_branch()
        self.assertEqual(value["github"]["deployment_branch"], "main")
        data = parse(render_workflow(value, tooling_ref=REF))
        self.assertEqual((data.get("on") or data["true"])["push"]["branches"], ["main"])

    def test_invalid_inputs_are_refused(self):
        for render in (render_workflow, render_deploy_workflow):
            for kwargs in ({"tooling_ref": "main"}, {"tooling_ref": REF[:-1]},
                           {"tooling_ref": REF, "platforms": "linux/386"},
                           {"tooling_ref": REF, "platforms": "windows/amd64"},
                           {"tooling_ref": REF, "tooling_repository": "evil repo; rm"}):
                with self.subTest(render=render.__name__, kwargs=kwargs), self.assertRaises(WorkflowError):
                    render(manifest(), **kwargs)
        with self.assertRaisesRegex(WorkflowError, "native"):
            render_deploy_workflow(manifest(runtime="native"), tooling_ref=REF)
        with self.assertRaisesRegex(WorkflowError, "native application needs"):
            render_workflow(manifest(runtime="native"), tooling_ref=REF)

    def test_the_quality_workflow_must_be_a_reusable_workflow_and_never_a_generated_file(self):
        for bad in ("code-quality.yml", "/.github/workflows/x.yml", ".github/workflows/../x.yml",
                    ".github/workflows/x.txt", ".github/workflows/x.yml@main", WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH):
            with self.subTest(bad=bad), self.assertRaisesRegex(WorkflowError, "quality workflow"):
                render_workflow(manifest(), tooling_ref=REF, quality_workflow=bad)
        text = render_workflow(manifest(), tooling_ref=REF, quality_workflow=".github/workflows/checks.yml")
        self.assertEqual(parse(text)["jobs"]["quality"]["uses"], "./.github/workflows/checks.yml")


STALE = "0" * 40


class ActionPinTests(unittest.TestCase):
    """Refreshing the pins in a workflow Shiplord wrote, without touching anything else."""

    def deploy_text(self):
        return render_deploy_workflow(manifest(), tooling_ref=REF)

    def stale_text(self):
        text = self.deploy_text()
        self.assertIn(PINS["checkout"].commit, text)
        return text.replace(PINS["checkout"].commit, STALE)

    def test_every_pin_names_a_repository_a_full_commit_and_the_release_it_belongs_to(self):
        for name, pin in PINS.items():
            with self.subTest(pin=name):
                self.assertEqual(ACTIONS[name], str(pin))
                self.assertRegex(pin.commit, r"^[0-9a-f]{40}$")
                self.assertEqual(pin.repository.count("/"), 1)
                self.assertTrue(pin.version)
                self.assertEqual(parse_action_pin(str(pin)), pin)
        for bad in ("actions/checkout@v4", "actions/checkout@" + STALE, "actions/checkout@" + STALE + " # v4 # x",
                    "no-slash/repo@x", ACTIONS["checkout"].replace(" # ", " ")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_action_pin(bad)

    def test_a_stale_pin_is_refreshed_and_every_other_line_is_preserved(self):
        text = self.stale_text() + "# a local note\n"
        upgraded, changes = upgrade_action_pins(text)
        self.assertEqual(upgraded, text.replace(STALE, PINS["checkout"].commit))
        self.assertEqual(changes, [PinChange("actions/checkout", STALE, PINS["checkout"].commit, "v4")])
        self.assertTrue(is_generated_workflow(upgraded))

    def test_upgrading_twice_changes_nothing_the_second_time(self):
        upgraded, _ = upgrade_action_pins(self.stale_text())
        self.assertEqual(upgrade_action_pins(upgraded), (upgraded, []))

    def test_a_floating_tag_for_an_action_shiplord_pins_is_replaced_by_the_commit(self):
        text = self.deploy_text().replace(f"actions/checkout@{PINS['checkout'].commit}", "actions/checkout@v4")
        upgraded, changes = upgrade_action_pins(text)
        self.assertIn(str(PINS["checkout"]), upgraded)
        self.assertEqual([change.previous for change in changes], ["v4"])

    def test_a_workflow_shiplord_did_not_write_is_refused(self):
        hand_written = ("name: Mine\non: push\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
                        f"      - uses: actions/checkout@{STALE}\n")
        for text in (hand_written, "# copied header\n" + self.deploy_text(), "\n" + self.deploy_text()):
            with self.subTest(text=text[:40]), self.assertRaisesRegex(WorkflowError, "generated by Shiplord"):
                upgrade_action_pins(text)

    def test_actions_and_local_workflows_shiplord_does_not_pin_are_left_alone(self):
        text = (self.stale_text() + "      - uses: example/other-action@" + STALE + " # v1\n"
                "      - uses: ./my/local-action\n")
        upgraded, changes = upgrade_action_pins(text)
        self.assertIn(f"example/other-action@{STALE} # v1", upgraded)
        self.assertIn("./my/local-action", upgraded)
        self.assertEqual([change.repository for change in changes], ["actions/checkout"])

    def test_the_trigger_workflow_is_accepted_and_needs_no_refresh(self):
        text = render_workflow(manifest(github={"repository": "Example/orders", "deployment_branch": "release/prod"}),
                               tooling_ref=REF, quality_workflow=DEFAULT_QUALITY_WORKFLOW)
        self.assertEqual(upgrade_action_pins(text), (text, []))
        self.assertIn('branches: ["release/prod"]', text)


class PinCheckTests(unittest.TestCase):
    """The resolver, driven by a fake HTTP client: the unit suite never uses the network."""

    def setUp(self):
        self.calls = []

    def fetch(self, answers):
        def fetch(url, token):
            self.calls.append((url, token))
            answer = answers.get(url, answers.get(None, (200, "{}")))
            if isinstance(answer, Exception):
                raise answer
            return answer
        return fetch

    def url(self, name):
        pin = PINS[name]
        return f"https://api.github.com/repos/{pin.repository}/commits/{pin.commit}"

    def test_every_pin_is_asked_about_in_its_own_repository(self):
        code, result = check_action_pins(fetch=self.fetch({}))
        self.assertEqual((code, result["ok"], result["checked"]), (0, True, len(PINS)))
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["unverified"], [])
        self.assertNotIn("error", result)
        self.assertEqual(sorted(url for url, _ in self.calls), sorted(self.url(name) for name in PINS))

    def test_a_pin_whose_commit_is_gone_fails_the_check_and_points_at_the_refresh(self):
        code, result = check_action_pins(fetch=self.fetch({self.url("build"): (404, "{}")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual(result["missing"], [f"{PINS['build'].repository}@{PINS['build'].commit} (v5)"])
        self.assertEqual(result["unverified"], [])
        self.assertIn("--upgrade-workflow", result["error"])
        states = {pin["action"]: pin["state"] for pin in result["pins"]}
        self.assertEqual((states["docker/build-push-action"], states["actions/checkout"]), ("missing", "resolves"))

    def test_a_rejected_reference_is_reported_as_missing_too(self):
        code, result = check_action_pins(fetch=self.fetch({self.url("python"): (422, "{}")}))
        self.assertEqual(code, 1)
        self.assertEqual(len(result["missing"]), 1)

    def test_without_the_api_nothing_is_reported_as_resolving(self):
        code, result = check_action_pins(fetch=self.fetch({None: OSError("no route to host")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["unverified"]), len(PINS))
        self.assertIn("nothing here says they resolve", result["error"])
        self.assertNotIn("resolves", {pin["state"] for pin in result["pins"]})

    def test_a_token_is_sent_but_never_echoed_in_the_result(self):
        token = "ghs_exampleTOKEN1234567890"
        code, result = check_action_pins(fetch=self.fetch({}), token=token)
        self.assertEqual(code, 0)
        self.assertEqual({sent for _, sent in self.calls}, {token})
        self.assertNotIn(token, json.dumps(result))

    def test_a_rate_limit_is_unverified_and_names_the_answer(self):
        code, result = check_action_pins(fetch=self.fetch({None: (403, "API rate limit exceeded")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual({pin["detail"] for pin in result["pins"]}, {"the GitHub API answered 403"})
        self.assertNotIn("rate limit exceeded", json.dumps(result))


REPOSITORY = Path(__file__).resolve().parent.parent


class RepositoryCiTests(unittest.TestCase):
    """This repository's own CI runs the pin check, and pins its own actions the same way."""

    def setUp(self):
        self.text = (REPOSITORY / ".github" / "workflows" / "test.yml").read_text()

    def test_ci_runs_the_pin_check_with_the_repository_token(self):
        self.assertIn("shiplord github check-pins --json", self.text)
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", self.text)
        self.assertIn("  action-pins:", self.text)

    def test_every_action_this_repository_uses_is_pinned_to_a_full_commit(self):
        references = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference=reference):
                self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$")


class CompositeActionTests(unittest.TestCase):
    """The generated workflow and ``deploy/action.yml`` must agree on the action's input contract.

    These assertions read the files without a YAML library, so they run on every machine, not only where
    the independent ruby parser above is installed.
    """

    def setUp(self):
        self.text = render_deploy_workflow(manifest(), tooling_ref=REF)
        reference = re.search(r"uses: (\S+/deploy@[0-9a-f]{40})", self.text)
        self.assertIsNotNone(reference, "the generated deploy workflow does not call the composite action")
        self.reference = reference.group(1)
        self.passed = passed_inputs(self.text, self.reference)
        self.declared = declared_inputs(ACTION_PATH)

    def test_the_action_is_pinned_to_the_commit_whose_shiplord_the_workflow_installs(self):
        self.assertEqual(self.reference, f"QueryPlanner/shiplord/deploy@{REF}")
        self.assertIn(f"git+https://github.com/QueryPlanner/shiplord.git@{REF}", self.text)

    def test_pypi_installs_use_a_separate_existing_action_pin(self):
        text = render_deploy_workflow(manifest(), tooling_version="0.2.0")
        reference = "QueryPlanner/shiplord/deploy@465a02fd0d76308546893d2cc37a94254e07c598"
        self.assertIn(f"uses: {reference}", text)
        self.assertIn('"shiplord==0.2.0"', text)
        self.assertNotIn("@None", text)
        self.assertNotIn("deploy@v0.2.0", text)
        self.assertEqual(passed_inputs(text, reference), self.passed)
        # A custom source repository only affects git installs, not the public PyPI package's action.
        self.assertIn(f"uses: {reference}", render_deploy_workflow(
            manifest(), tooling_version="0.2.0", tooling_repository="Example/fork"))

    def test_every_input_the_workflow_passes_is_declared_and_nothing_undeclared_is_passed(self):
        self.assertTrue(self.passed)
        self.assertEqual(set(self.passed), set(self.declared))
        for name, value in self.passed.items():
            self.assertTrue(value, f"the workflow passes no value for {name}")
            self.assertTrue(self.declared[name].get("description"), name)
            self.assertIn(self.declared[name].get("required"), ("true", "false"), name)

    def test_the_required_flag_of_every_input_is_the_contract_the_workflow_relies_on(self):
        required = {name for name, spec in self.declared.items() if spec.get("required") == "true"}
        self.assertEqual(required, {"ssh-private-key", "ssh-known-hosts", "deploy-user", "server-host",
                                    "ts-client-id", "ts-audience", "image", "digest", "commit", "request-id"})
        self.assertEqual(set(self.passed), required | {"check-only", "ssh-port", "ts-tags"})
        for name, spec in self.declared.items():
            if name not in required:
                self.assertIn("default", spec, name)  # an optional input must say what it falls back to
        # Every required input is supplied, so the action never runs with a missing credential or digest.
        self.assertEqual(set(self.passed) & required, required)


@unittest.skipUnless(shutil.which("ruby"), "ruby is used as an independent YAML parser")
class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = render_workflow(native_manifest(), tooling_ref=REF)
        self.data = parse(self.text)
        self.jobs = self.data["jobs"]

    def named(self, job):
        return {step.get("name"): step for step in self.jobs[job]["steps"]}

    def test_a_native_workflow_builds_uploads_and_deploys_the_binary(self):
        self.assertEqual(list(self.jobs), ["build", "deploy"])
        self.assertEqual(self.jobs["deploy"]["needs"], "build")
        build = self.named("build")
        self.assertIn("go build -trimpath -o dist/orders ./cmd/orders", build["Build the binary"]["run"])
        self.assertEqual(build["Build the binary"]["env"]["BINARY_PATH"], "dist/orders")
        upload = build["Upload the binary"]
        self.assertEqual(upload["with"]["path"], "dist/orders")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        deploy = self.named("deploy")
        download = deploy["Download the binary"]
        self.assertEqual(download["with"]["name"], upload["with"]["name"])
        self.assertEqual(download["with"]["path"], "${{ runner.temp }}/shiplord-binary")
        self.assertEqual(deploy["Deploy"]["env"]["BINARY"], "${{ runner.temp }}/shiplord-binary/orders")
        for fragment in ('shiplord deploy --binary "$BINARY"', '--commit "$GITHUB_SHA"',
                         '--request-id "gh-${RUN_ID}-${RUN_ATTEMPT}"', "--require-public", "--json"):
            self.assertIn(fragment, deploy["Deploy"]["run"])

    def test_a_native_workflow_pins_the_same_actions_as_the_docker_one(self):
        for reference in re.findall(r"uses: (\S+)", self.text):
            self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$", reference)
        for name in ("checkout", "python", "tailscale", "upload-artifact", "download-artifact"):
            self.assertIn(ACTIONS[name], self.text)
        docker = render_workflow(manifest(), tooling_ref=REF)
        self.assertNotIn("upload-artifact", docker)  # the Docker workflow is unchanged
        self.assertNotIn("download-artifact", docker)

    def test_a_check_only_dispatch_skips_the_build_and_the_download(self):
        self.assertEqual(self.jobs["build"]["if"], "${{ !inputs.check_only }}")
        deploy = self.named("deploy")
        self.assertEqual(deploy["Download the binary"]["if"], "${{ !inputs.check_only }}")
        self.assertEqual(deploy["Deploy"]["if"], "${{ !inputs.check_only }}")
        self.assertEqual(deploy["Check access only"]["if"], "${{ inputs.check_only }}")
        self.assertIn("needs.build.result == 'success'", self.jobs["deploy"]["if"])

    def test_the_build_runs_on_the_runner_for_the_binary_architecture(self):
        self.assertEqual(self.jobs["build"]["runs-on"], "ubuntu-24.04")
        arm = parse(render_workflow(native_manifest(), tooling_ref=REF, platforms="linux/arm64"))
        self.assertEqual(arm["jobs"]["build"]["runs-on"], "ubuntu-24.04-arm")

    def test_no_secret_variable_or_event_value_is_interpolated_into_a_shell_script(self):
        for job in self.jobs.values():
            for step in job.get("steps", []):
                if "run" in step:
                    self.assertNotIn("${{", step["run"], step.get("name"))
        self.assertEqual(self.text.count("secrets.SHIPLORD_SSH_PRIVATE_KEY"), 1)

    def test_the_native_workflow_is_deterministic_and_follows_the_deployment_branch(self):
        self.assertEqual(self.text, render_workflow(native_manifest(), tooling_ref=REF))
        other = native_manifest(github={"repository": "Example/orders", "deployment_branch": "release/prod",
                                        "build_command": "make", "binary_path": "build/orders"})
        data = parse(render_workflow(other, tooling_ref=REF))
        self.assertEqual((data.get("on") or data["true"])["push"]["branches"], ["release/prod"])
        self.assertIn("github.ref == 'refs/heads/release/prod'", data["jobs"]["deploy"]["if"])
        deploy = next(s for s in data["jobs"]["deploy"]["steps"] if s.get("name") == "Deploy")
        self.assertEqual(deploy["env"]["BINARY"], "${{ runner.temp }}/shiplord-binary/orders")

    def test_missing_or_unsafe_build_settings_are_refused(self):
        base = {"repository": "Example/orders", "deployment_branch": "main"}
        for github in (base,
                       base | {"build_command": "make"},
                       base | {"binary_path": "dist/orders"},
                       base | {"build_command": "make\nrm -rf /", "binary_path": "dist/orders"},
                       base | {"build_command": "make", "binary_path": "/usr/local/bin/orders"},
                       base | {"build_command": "make", "binary_path": "../orders"},
                       base | {"build_command": "make", "binary_path": "dist//orders"},
                       base | {"build_command": "", "binary_path": "dist/orders"}):
            with self.subTest(github=github), self.assertRaises(WorkflowError):
                render_workflow(native_manifest(github=github), tooling_ref=REF)
        with self.assertRaisesRegex(WorkflowError, "one binary"):
            render_workflow(native_manifest(), tooling_ref=REF, platforms="linux/amd64,linux/arm64")



if __name__ == "__main__":
    unittest.main()
