"""Every command has a readable default rendering and a stable ``--json`` form.

The golden table below is the contract: one exact JSON document and one readable
rendering per command. ``test_every_command_has_a_case`` fails when a command is
added without both, so the table cannot silently fall behind the CLI.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, NamedTuple
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from shiplord import __version__, cli, output
from shiplord.workflow import PINS

IMAGE = "ghcr.io/example/orders@sha256:" + "a" * 64
COMMIT = "b" * 40
MANIFEST = {"name": "orders", "server": "production"}
CLIENT = {"version": __version__, "executable": "/opt/shiplord/bin/shiplord"}

INVENTORY = {"ok": True, "applications": [{
    "application": "orders", "domain": "orders.example.com", "runtime": "docker", "host_port": 23001,
    "release_recorded": True, "unfinished_transaction": None, "ok": True,
}]}

PROVISIONED = {
    "ok": True, "application_id": "orders", "deployment_user": "ship-orders", "host_port": 23001,
    "api_domain": "orders.example.com", "runtime": "docker", "max_memory_mb": 512,
    "key_fingerprint": "SHA256:abc", "created": {"account": True, "key": True, "release_state": False},
    "deploy_login": "ship-orders@host.example.com", "key_label": "local-abcdef12",
    "next": "Point DNS for the API hostname at the server, then run `shiplord deploy`.",
}
DEPLOYED = {
    "ok": True, "request_id": "deploy-1", "status": "succeeded", "exit_code": 0,
    "release": {"commit": COMMIT, "artifact": IMAGE}, "url": "https://orders.example.com",
    "public": {"ok": True, "status": 200, "error": None}, "warnings": ["cleanup skipped"],
}
RELEASED = {**DEPLOYED, "build": {"repository": "ghcr.io/example/orders", "tag": COMMIT, "platform": "linux/amd64",
                                "revision": COMMIT, "digest": "sha256:" + "a" * 64, "image": IMAGE}}
PLANNED = {
    "ok": True, "scope": "server plan; nothing was changed", "action": "deploy",
    "candidate": {"commit": COMMIT, "sequence": 1001, "artifact": IMAGE},
    "current": None, "restarts_application": True, "route_change": "create",
    "domain": "orders.example.com",
}
STATUS = {"ok": True, "application": {
    "application_id": "orders", "current": {"commit": COMMIT, "artifact": IMAGE},
    "previous": None, "unfinished_transaction": None, "running": True,
}}
ROLLED_BACK = {
    "ok": True, "request_id": "rollback-1", "status": "succeeded", "exit_code": 0,
    "release": {"commit": COMMIT, "artifact": IMAGE}, "previous": {"commit": "c" * 40, "artifact": IMAGE},
    "warnings": [],
}
RECOVERED = {
    "ok": True, "request_id": "recover-1", "status": "recovered", "exit_code": 0,
    "target": {"commit": COMMIT, "artifact": IMAGE},
    "warnings": ["the interrupted candidate was removed"],
}
DEPROVISIONED = {
    "ok": True, "application_id": "orders", "removed": {"registration": True, "keys": False},
    "retained": ["data directory and runtime account (they hold application data)"],
    "note": "Application data is never deleted; remove it by hand when you are sure.",
}
ENVIRONMENT = {"ok": True, "keys": ["TOKEN"], "changed": True,
               "note": "Takes effect the next time a release starts."}
REGISTRY = {"ok": True, "registries": [{"registry": "ghcr.io", "username": "bot"}],
            "note": "Used for pulling this application's private images. Use a read-only token."}
GITHUB_SETUP = {
    "ok": True, "repository": "QueryPlanner/orders", "github_account": "bot", "key_label": "ci-abc",
    "key_fingerprint": "SHA256:def", "server_host": "host.example.com",
    "steps": {"secret": "SHIPLORD_SSH_KEY stored", "variables": "SHIPLORD_SERVER_HOST"},
    "remaining": ["No GitHub Actions run has been performed"],
}
GITHUB_STATUS = {"ok": False, "repository": "QueryPlanner/orders", "checks": {
    "access": {"ok": True, "account": "bot", "permission": "admin"},
    "workflow": {"ok": False, "unmodified_since_generation": False},
}}
GITHUB_CHECK = {"ok": True, "run": "https://github.com/QueryPlanner/orders/actions/runs/1",
                "conclusion": "success", "steps": {"Check access only": "success"}}
GITHUB_ROTATED = {"ok": True, "repository": "QueryPlanner/orders", "old_label": "ci-old",
                  "key_label": "ci-new", "key_fingerprint": "SHA256:new", "steps": {"register": "ci-new"}}
RESTARTED = {"ok": True, "request_id": "restart-1", "status": "succeeded", "exit_code": 0}
PIN_CHECK = {"ok": True, "checked": len(PINS), "missing": [], "unverified": [],
             "pins": [{"action": pin.repository, "commit": pin.commit, "version": pin.version, "state": "resolves"}
                      for pin in PINS.values()]}
KEYS = {"keys": [{"label": "ci", "fingerprint": "SHA256:abc"}]}
BACKED_UP = {"ok": True, "status": "succeeded", "archive": "shiplord-backup-orders-20240101T000000Z.tar.gz",
             "sha256": "b" * 64, "size": 10, "entries": 2, "bytes": 4, "verified": True,
             "application": {"restarted": True, "running": True}}
UNINSTALLED = {
    "status": "uninstalled",
    "removed": {"caddy": {"import": True, "placeholder": False}, "sshd": True, "boot_recovery": True,
                "wrappers": ["shiplord-host", "shiplord-admin"], "versions": 2, "directories": ["registrations"]},
    "retained": ["the state directory that holds the host lock"], "applications": [],
}
SETUP = {"ok": True, "resumed_at": "configure", "steps": [
    {"step": "configure", "status": "ran", "result": {"answers": {}}},
    {"step": "provision", "status": "already complete"},
    {"step": "environment", "status": "not requested",
     "detail": "no --env-file given; runtime environment was not changed"},
    {"step": "registry", "status": "not requested",
     "detail": "no registry token given; registry login was not changed"},
    {"step": "github", "status": "not requested", "detail": "--skip-ci given; CI was not configured"},
    {"step": "check-ci", "status": "not requested", "detail": "--skip-ci given; CI was not checked"},
]}

# A dumped dict or list: a line, or the value after its first label, starts with a structure.
STRUCTURE_DUMP = ("OrderedDict(", "dict_keys(", "dict_values(")


def looks_like_dump(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            return True
        if ": " in stripped and stripped.split(": ", 1)[1].strip().startswith(("{", "[")):
            return True
        if any(marker in stripped for marker in STRUCTURE_DUMP):
            return True
    return False


@contextmanager
def chdir(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class Case(NamedTuple):
    name: str
    invoke: Callable[[bool], object]
    expected: dict
    tokens: tuple[str, ...] = ()
    exit_code: int = 0
    raw_stdout: bool = False  # logs pass application output through untouched


class CommandOutputTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.config = self.enterContext(tempfile.TemporaryDirectory())
        self.env = dict(os.environ, XDG_CONFIG_HOME=self.config)
        self.runner = CliRunner()

    def invoke(self, args, as_json, *, patches=(), servers=(), cwd=None, input=None, xdg=None):
        argv = [*args, "--json"] if as_json else list(args)
        if servers:
            self.write_servers(*servers)
        env = dict(self.env, XDG_CONFIG_HOME=xdg) if xdg else self.env
        with ExitStack() as stack:
            for target, kwargs in patches:
                stack.enter_context(patch(target, **kwargs))
            if cwd is not None:
                stack.enter_context(chdir(cwd))
            return self.runner.invoke(cli.app, argv, env=env, input=input)

    def write_servers(self, *servers):
        directory = Path(self.config) / "shiplord" / "servers"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for path in (Path(self.config), directory.parent, directory):
            path.chmod(0o700)
        for alias, ssh in servers:
            path = directory / f"{alias}.json"
            path.write_text(json.dumps({"schema_version": 1, "alias": alias, "ssh": ssh}))
            path.chmod(0o600)

    def with_temp_file(self, name: str, contents: str, invoke):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_text(contents)
            return invoke(path)

    def cases(self) -> list[Case]:
        manifest = dict(MANIFEST)
        return [
            Case("apps", lambda as_json: self.invoke(
                ["apps", "production"], as_json, servers=[("production", "admin@example.com")],
                patches=[("shiplord.cli.probe_capabilities", {"return_value": {"helper_version": "0.2.0"}}),
                         ("shiplord.cli.commands.server_applications", {"return_value": (0, INVENTORY)})]),
                {**INVENTORY, "server": "production"},
                ("Server: production", "Outcome: complete", "orders.example.com")),
            Case("plan", lambda as_json: self.invoke(
                ["plan"], as_json, patches=[("shiplord.cli._manifest", {"return_value": manifest})]),
                {"ok": True, "scope": "local configuration preview",
                 "remote_changes": "unknown; pass --image or --binary to ask the server", "configuration": manifest},
                ("Target: local configuration", "Outcome: preview ready")),
            Case("plan --image", lambda as_json: self.invoke(
                ["plan", "--image", IMAGE, "--commit", COMMIT], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli.commands.validate_deploy_inputs", {}),
                         ("shiplord.cli.commands.commit_sequence", {"return_value": 1001}),
                         ("shiplord.cli.commands.plan_application", {"return_value": (0, PLANNED)})]),
                PLANNED, ("Plan action: deploy", "Route change: create")),
            Case("doctor --local", lambda as_json: self.invoke(
                ["doctor", "--local"], as_json,
                patches=[("shiplord.cli._client_details", {"return_value": dict(CLIENT)}),
                         ("shiplord.cli.shutil.which", {"side_effect": lambda name: f"/usr/bin/{name}"})]),
                {"ok": True, "scope": "local prerequisites only; server readiness was not checked",
                 "checks": {"ssh": True, "git": True, "gh": True}, "client": CLIENT},
                ("Target: local machine", "Outcome: ready", "ssh: available")),
            Case("doctor --server", lambda as_json: self.invoke(
                ["doctor", "--server", "production"], as_json,
                patches=[("shiplord.cli.load_server", {"return_value": "admin@example.com"}),
                         ("shiplord.cli.probe_capabilities", {"return_value": {"helper_version": "0.2.0"}}),
                         ("shiplord.cli._client_details", {"return_value": dict(CLIENT)})]),
                {"ok": True,
                 "scope": "protocol capabilities only; server readiness and deployment access were not checked",
                 "helper_version": "0.2.0", "client": CLIENT},
                ("Target: server production", "Outcome: protocol reachable", "Server helper: 0.2.0")),
            Case("doctor --server --deep", lambda as_json: self.invoke(
                ["doctor", "--server", "production", "--deep"], as_json,
                patches=[("shiplord.cli.load_server", {"return_value": "admin@example.com"}),
                         ("shiplord.cli.commands.server_doctor",
                          {"return_value": (0, {"ok": True, "scope": "server health (read-only)",
                                                "checks": {"caddy": {"ok": True}}})}),
                         ("shiplord.cli._client_details", {"return_value": dict(CLIENT)})]),
                {"ok": True, "scope": "server health (read-only)", "checks": {"caddy": {"ok": True}},
                 "client": CLIENT},
                ("Outcome: ready", "Server services:", "caddy: passed")),
            Case("doctor", lambda as_json: self.invoke(
                ["doctor"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._manifest_present", {"return_value": True}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli.commands.application_doctor",
                          {"return_value": (0, {"ok": True, "scope": "local tools, SSH access, helper, "
                                                           "application, and DNS",
                                                "checks": {"server_access": {"ok": True, "helper_version": "0.2.0"}}})}),
                         ("shiplord.cli._client_details", {"return_value": dict(CLIENT)})]),
                {"ok": True, "scope": "local tools, SSH access, helper, application, and DNS",
                 "checks": {"server_access": {"ok": True, "helper_version": "0.2.0"}}, "client": CLIENT},
                ("Target: orders on production", "server access: passed")),
            Case("server add", lambda as_json: self.invoke(
                ["server", "add", "production", "--ssh", "admin@example.com"], as_json,
                patches=[("shiplord.cli.register_server", {"return_value": None})]),
                {"ok": True, "alias": "production", "ssh": "admin@example.com"},
                ("Server alias saved: production", "shiplord server bootstrap")),
            Case("server list", lambda as_json: self.invoke(
                ["server", "list"], as_json,
                patches=[("shiplord.cli.list_servers",
                          {"return_value": ([{"alias": "production", "ssh": "admin@example.com"}], 0)})]),
                {"ok": True, "servers": [{"alias": "production", "ssh": "admin@example.com"}],
                 "invalid_entries": 0},
                ("production  admin@example.com",)),
            Case("server bootstrap", lambda as_json: self.invoke(
                ["server", "bootstrap", "production"], as_json,
                patches=[("shiplord.cli.load_server", {"return_value": "admin@example.com"}),
                         ("shiplord.cli.bootstrap_server",
                          {"return_value": {"version": "0.2.0-abc", "changed": {"code": True, "sshd": False}}})]),
                {"ok": True, "version": "0.2.0-abc", "changed": {"code": True, "sshd": False}},
                ("Target: server production", "Helper version: 0.2.0-abc", "Changed: code")),
            Case("init", lambda as_json: self.invoke(
                ["init", "orders", "--template", "google-adk"], as_json,
                patches=[("shiplord.cli.create_project", {"return_value": Path("/tmp/orders")})]),
                {"ok": True, "name": "orders", "template": "google-adk"},
                ("Created orders from the pinned google-adk template.",)),
            Case("configure", self.configure_case,
                {"ok": True, "name": "orders", "server": "production", "api_domain": "api.example.com",
                 "frontend_origins": [], "runtime": "docker", "template": "google-adk",
                 "container_port": 8080, "readiness_path": "/ready", "memory_mb": 512, "registered": False},
                ("Outcome: ship.toml created", "The server alias has not been checked.")),
            Case("setup", lambda as_json: self.invoke(
                ["setup", "--server", "production", "--api-domain", "api.example.com", "--no-frontend",
                 "--repo", "QueryPlanner/orders", "--skip-ci"], as_json,
                patches=[("shiplord.cli.setup_module.run_setup", {"return_value": (0, SETUP)}),
                         ("shiplord.cli.load_manifest", {"return_value": dict(MANIFEST)})]),
                {"application": "orders", "server": "production", **SETUP},
                ("Target: orders on production", "configure: done", "provision: already complete",
                 "environment: not requested (no --env-file given; runtime environment was not changed)",
                 "Outcome: ready")),
            Case("provision", lambda as_json: self.invoke(
                ["provision"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._admin", {}),
                         ("shiplord.cli.commands.provision_application", {"return_value": (0, PROVISIONED)})]),
                PROVISIONED,
                ("Deployment login: ship-orders@host.example.com", "Created: account, key",
                 "Already present: release state", "Next: Point DNS")),
            Case("deploy", lambda as_json: self.invoke(
                ["deploy", "--image", IMAGE, "--commit", COMMIT], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli.commands.validate_deploy_inputs", {}),
                         ("shiplord.cli.commands.commit_sequence", {"return_value": 1001}),
                         ("shiplord.cli.commands.deploy_application", {"return_value": (0, DEPLOYED)})]),
                DEPLOYED, ("Outcome: succeeded", "Public HTTPS: passed", "Warning: cleanup skipped")),
            Case("release", lambda as_json: self.invoke(
                ["release"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli._head_commit", {"return_value": COMMIT}),
                         ("shiplord.cli.commands.commit_sequence", {"return_value": 1001}),
                         ("shiplord.cli.commands.release_application", {"return_value": (0, RELEASED)})]),
                RELEASED, ("Outcome: succeeded", "Image: " + IMAGE, "Platform: linux/amd64")),
            Case("status", lambda as_json: self.invoke(
                ["status", "orders", "--server", "production"], as_json,
                patches=[("shiplord.cli._app_context", {"return_value": ("orders", "production", object())}),
                         ("shiplord.cli.commands.application_status", {"return_value": (0, STATUS)})]),
                STATUS, ("Target: orders on production", "Outcome: running",
                         "Current release: bbbbbbbbbbbb")),
            Case("logs", lambda as_json: self.invoke(
                ["logs", "orders", "--server", "production"], as_json,
                patches=[("shiplord.cli._app_context", {"return_value": ("orders", "production", object())}),
                         ("shiplord.cli.commands.application_logs",
                          {"return_value": (0, {"ok": True, "logs": "one\ntwo\n"})})]),
                {"ok": True, "logs": "one\ntwo\n"}, ("Outcome: retrieved 2 log lines",), raw_stdout=True),
            Case("rollback", lambda as_json: self.invoke(
                ["rollback"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli.commands.run_job_command", {"return_value": (0, ROLLED_BACK)})]),
                ROLLED_BACK, ("Outcome: succeeded", "Release: bbbbbbbbbbbb", "Previous release: cccccccccccc")),
            Case("recover", lambda as_json: self.invoke(
                ["recover"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli.commands.run_job_command", {"return_value": (0, RECOVERED)})]),
                RECOVERED, ("Outcome: recovered", "Recovered to: bbbbbbbbbbbb",
                            "Warning: the interrupted candidate was removed")),
            Case("deprovision", lambda as_json: self.invoke(
                ["deprovision", "--yes"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._admin", {}),
                         ("shiplord.cli.commands.deprovision_application",
                          {"return_value": (0, DEPROVISIONED)})]),
                DEPROVISIONED, ("Outcome: access removed", "Retained: data directory", "Note: Application data")),
            Case("env set", lambda as_json: self.with_temp_file(
                "app.env", "TOKEN=secret-value\n",
                lambda path: self.invoke(["env", "set", "--from-file", str(path)], as_json,
                                         patches=self.admin_patches(
                                             "shiplord.cli.commands.set_environment", (0, ENVIRONMENT)))),
                ENVIRONMENT, ("Outcome: environment saved", "Variables: 1 (TOKEN)")),
            Case("env unset", lambda as_json: self.invoke(
                ["env", "unset", "OLD"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.edit_environment",
                                           (0, {"ok": True, "keys": ["KEEP"], "changed": True,
                                                "note": "Takes effect the next time a release starts."}))),
                {"ok": True, "keys": ["KEEP"], "changed": True,
                 "note": "Takes effect the next time a release starts."},
                ("Outcome: variable removed", "Variables: 1 (KEEP)", "Note: Takes effect")),
            Case("env keys", lambda as_json: self.invoke(
                ["env", "keys"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.set_environment",
                                           (0, {"ok": True, "keys": ["TOKEN"]}))),
                {"ok": True, "keys": ["TOKEN"]}, ("Outcome: 1 variable configured", "  TOKEN")),
            Case("env clear", lambda as_json: self.invoke(
                ["env", "clear"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.set_environment",
                                           (0, {"ok": True, "keys": [], "changed": True,
                                                "note": "Takes effect the next time a release starts."}))),
                {"ok": True, "keys": [], "changed": True, "note": "Takes effect the next time a release starts."},
                ("Outcome: environment removed", "Variables: 0")),
            Case("registry login", lambda as_json: self.with_temp_file(
                "token", "ghp_fixture\n",
                lambda path: self.invoke(["registry", "login", "--username", "bot", "--token-file", str(path)],
                                         as_json, patches=self.admin_patches(
                                             "shiplord.cli.commands.registry_login", (0, REGISTRY)))),
                REGISTRY, ("Outcome: registry login saved", "ghcr.io: bot")),
            Case("registry logout", lambda as_json: self.invoke(
                ["registry", "logout"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.registry_other",
                                           (0, {"ok": True, "registries": []}))),
                {"ok": True, "registries": []}, ("Outcome: registry login removed",)),
            Case("registry status", lambda as_json: self.invoke(
                ["registry", "status"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.registry_other",
                                           (0, {"ok": True, "registries": REGISTRY["registries"]}))),
                {"ok": True, "registries": REGISTRY["registries"]},
                ("Outcome: 1 registry login", "ghcr.io: bot")),
            Case("github setup", lambda as_json: self.invoke(
                ["github", "setup", "--repo", "QueryPlanner/orders", "--tailscale-tags", "tag:ci"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli.github.validate_setup_request", {"return_value": None}),
                         ("shiplord.cli.load_server", {"return_value": "admin@example.com"}),
                         ("shiplord.cli.github.GitHub", {"return_value": MagicMock()}),
                         ("shiplord.cli.github.tailscale_credential", {"return_value": None}),
                         ("shiplord.cli.github.github_setup", {"return_value": dict(GITHUB_SETUP)})]),
                {**GITHUB_SETUP, "tailscale": {"source": "not configured", "warnings": []}},
                ("Outcome: CI configured", "Steps:", "secret: SHIPLORD_SSH_KEY stored", "Remaining:")),
            Case("github check-ci", lambda as_json: self.invoke(
                ["github", "check-ci"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli.github.GitHub", {"return_value": MagicMock()}),
                         ("shiplord.cli.github.check_ci", {"return_value": (0, GITHUB_CHECK)})]),
                GITHUB_CHECK, ("Outcome: check run succeeded", "Check access only: success")),
            Case("github status", lambda as_json: self.invoke(
                ["github", "status"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli.github.GitHub", {"return_value": MagicMock()}),
                         ("shiplord.cli.github.github_status", {"return_value": (0, GITHUB_STATUS)})]),
                GITHUB_STATUS, ("Outcome: attention required", "access: passed (account: bot; permission: admin)")),
            Case("github rotate-key", lambda as_json: self.invoke(
                ["github", "rotate-key"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli.load_server", {"return_value": "admin@example.com"}),
                         ("shiplord.cli.github.GitHub", {"return_value": MagicMock()}),
                         ("shiplord.cli.github.rotate_key", {"return_value": dict(GITHUB_ROTATED)})]),
                GITHUB_ROTATED, ("Outcome: CI key replaced", "New key: ci-new", "register: ci-new")),
            Case("github check-pins", lambda as_json: self.invoke(
                ["github", "check-pins"], as_json,
                patches=[("shiplord.workflow.github_fetch", {"return_value": (200, "{}")})]),
                PIN_CHECK, ("Outcome: succeeded", f"Checked: {len(PINS)}", "Pins:", "- see --json")),
            Case("restart", lambda as_json: self.invoke(
                ["restart"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._helper", {}),
                         ("shiplord.cli.commands.run_job_command", {"return_value": (0, RESTARTED)})]),
                RESTARTED, ("Outcome: succeeded", "Request ID: restart-1")),
            Case("backup", lambda as_json: self.invoke(
                ["backup"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.backup_application", (0, BACKED_UP))),
                BACKED_UP, ("Outcome: succeeded", "Archive: shiplord-backup-orders", "Verified: yes",
                            "Application: running")),
            Case("restore", lambda as_json: self.invoke(
                ["restore", "--latest", "--yes"], as_json,
                patches=self.admin_patches("shiplord.cli.commands.restore_application", (0, BACKED_UP))),
                BACKED_UP, ("Outcome: succeeded", "Archive: shiplord-backup-orders", "Verified: yes",
                            "Application: running")),
            Case("server uninstall", lambda as_json: self.invoke(
                ["server", "uninstall", "production"], as_json,
                patches=[("shiplord.cli.load_server", {"return_value": "admin@example.com"}),
                         ("shiplord.cli._admin_target", {"return_value": self.uninstall_admin()})]),
                {"ok": True, **UNINSTALLED,
                 "note": "Application data is never deleted; remove it by hand when you are sure."},
                ("Removed:", "the managed Caddy import", "the boot recovery unit",
                 "the launchers: shiplord-host, shiplord-admin",
                 "Left in place:", "Application data is never deleted", "shiplord server bootstrap")),
            Case("server remove", lambda as_json: self.invoke(
                ["server", "remove", "production"], as_json,
                patches=[("shiplord.cli.remove_server", {"return_value": None})]),
                {"ok": True, "removed": "production"}, ("Removed server alias 'production'.",)),
            Case("server rename", lambda as_json: self.invoke(
                ["server", "rename", "production", "staging"], as_json,
                patches=[("shiplord.cli.rename_server", {"return_value": None})]),
                {"ok": True, "alias": "staging", "previous_alias": "production"},
                ("Renamed server alias 'production' to 'staging'.",)),
            Case("env diff", lambda as_json: self.with_temp_file(
                "app.env", "TOKEN=secret-value\n",
                lambda path: self.invoke(["env", "diff", "--from-file", str(path)], as_json,
                                         patches=self.admin_patches(
                                             "shiplord.cli.commands.diff_environment",
                                             (0, {"ok": True, "added": ["TOKEN"], "removed": [],
                                                  "note": "Key names only."})))),
                {"ok": True, "added": ["TOKEN"], "removed": [], "note": "Key names only."},
                ("Added: TOKEN", "Removed: none", "Key names only.")),
            Case("keys add", lambda as_json: self.invoke(
                ["keys", "add", "--label", "ci", "--public-key-stdin"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._admin", {"return_value": self.keys_admin()}),
                         ("shiplord.cli._read_public_key", {"return_value": ("ssh-ed25519 AAAA", "SHA256:abc")})]),
                {"ok": True, **KEYS, "label": "ci", "fingerprint": "SHA256:abc"},
                ("Outcome: key ci registered", "ci: SHA256:abc")),
            Case("keys list", lambda as_json: self.invoke(
                ["keys", "list"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._admin", {"return_value": self.keys_admin()})]),
                {"ok": True, **KEYS}, ("Outcome: 1 key", "ci: SHA256:abc")),
            Case("keys remove", lambda as_json: self.invoke(
                ["keys", "remove", "--label", "ci", "--yes"], as_json,
                patches=[("shiplord.cli._manifest", {"return_value": manifest}),
                         ("shiplord.cli._admin", {"return_value": self.keys_admin()})]),
                {"ok": True, **KEYS, "label": "ci"}, ("Outcome: key ci removed", "ci: SHA256:abc")),
        ]

    @staticmethod
    def uninstall_admin():
        """An admin client whose only request reports what the server removed."""
        admin = MagicMock()
        admin.call.return_value = dict(UNINSTALLED)
        return admin

    @staticmethod
    def keys_admin():
        """An admin client whose only request returns the fixture key list."""
        admin = MagicMock()
        admin.call.return_value = dict(KEYS)
        return admin

    def admin_patches(self, target: str, result: tuple[int, dict]):
        return [("shiplord.cli._manifest", {"return_value": dict(MANIFEST)}),
                ("shiplord.cli._admin", {}),
                (target, {"return_value": result})]

    def configure_case(self, as_json: bool):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            return self.invoke(
                ["configure", "--name", "orders", "--server", "production", "--api-domain", "api.example.com",
                 "--no-frontend"], as_json, cwd=directory, xdg=config)

    def test_every_json_document_is_golden(self):
        for case in self.cases():
            with self.subTest(command=case.name):
                result = case.invoke(True)
                self.assertEqual(result.exit_code, case.exit_code, result.output)
                self.assertEqual(result.stdout, json.dumps(case.expected, sort_keys=True) + "\n")

    def test_every_command_renders_readable_lines(self):
        for case in self.cases():
            with self.subTest(command=case.name):
                result = case.invoke(False)
                self.assertEqual(result.exit_code, case.exit_code, result.output)
                rendered = result.stderr if case.raw_stdout else result.output
                self.assertTrue(rendered.strip(), result.output)
                self.assertFalse(looks_like_dump(rendered), f"raw structure in {case.name} output:\n{rendered}")
                for token in case.tokens:
                    self.assertIn(token, result.output)

    def test_every_command_has_a_case(self):
        registered = {command.name or command.callback.__name__ for command in cli.app.registered_commands}
        for group in cli.app.registered_groups:
            for command in group.typer_instance.registered_commands:
                registered.add(f"{group.name} {command.name}")
        covered = {case.name.split(" --")[0] for case in self.cases()}
        self.assertEqual(registered - covered, set(), "a command has no golden case")
        self.assertEqual(covered - registered, set(), "a case names a command that does not exist")

    def test_logs_human_output_passes_application_text_through(self):
        case = next(case for case in self.cases() if case.name == "logs")
        result = case.invoke(False)
        self.assertEqual(result.stdout, "one\ntwo\n")
        self.assertIn("Outcome: retrieved 2 log lines", result.stderr)

    def test_configure_json_never_prompts(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config:
            with patch("shiplord.cli._interactive", return_value=True):
                result = self.invoke(["configure", "--name", "orders", "--server", "production",
                                      "--api-domain", "api.example.com", "--no-frontend"], True, cwd=directory,
                                     xdg=config)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["name"], "orders")


class FailureOutputTests(unittest.TestCase):
    """Failures keep the documented exit codes and are readable in both forms."""

    def setUp(self):
        self.config = self.enterContext(tempfile.TemporaryDirectory())
        self.env = dict(os.environ, XDG_CONFIG_HOME=self.config)

    def invoke(self, args, as_json, patches=()):
        argv = [*args, "--json"] if as_json else list(args)
        with ExitStack() as stack:
            for target, kwargs in patches:
                stack.enter_context(patch(target, **kwargs))
            return CliRunner().invoke(cli.app, argv, env=self.env)

    def test_documented_exit_codes_and_readable_errors(self):
        cases = [
            ("invalid input", ["deploy"], [], 2, "exactly one of --image and --binary"),
            ("missing prerequisite", ["provision"], [("shiplord.cli._manifest", {"return_value": dict(MANIFEST)})], 3,
             "could not access the private server configuration directory"),
            ("failed job", ["rollback"],
             [("shiplord.cli._manifest", {"return_value": dict(MANIFEST)}), ("shiplord.cli._helper", {}),
              ("shiplord.cli.commands.run_job_command",
               {"return_value": (1, {"ok": False, "request_id": "r", "status": "failed", "exit_code": 1,
                                     "error": "no_previous_release"})})],
             1, "no previous release"),
            ("rolled back", ["deploy", "--image", IMAGE, "--commit", COMMIT],
             [("shiplord.cli._manifest", {"return_value": dict(MANIFEST)}), ("shiplord.cli._helper", {}),
              ("shiplord.cli.commands.validate_deploy_inputs", {}),
              ("shiplord.cli.commands.commit_sequence", {"return_value": 1001}),
              ("shiplord.cli.commands.deploy_application",
               {"return_value": (5, {"ok": False, "request_id": "d", "status": "rolled_back", "exit_code": 5,
                                     "error": "prepare_failed", "restored": True})})],
             5, "rolled back"),
            ("intervention", ["recover"],
             [("shiplord.cli._manifest", {"return_value": dict(MANIFEST)}), ("shiplord.cli._helper", {}),
              ("shiplord.cli.commands.run_job_command",
               {"return_value": (6, {"ok": False, "request_id": "r", "status": "intervention_required",
                                     "exit_code": 6, "error": "recovery_failed", "message": "stop it first"})})],
             6, "shiplord recover"),
            ("incompatible helper", ["apps", "production"],
             [("shiplord.cli.list_servers",
               {"return_value": ([{"alias": "production", "ssh": "admin@example.com"}], 0)}),
              ("shiplord.cli.probe_capabilities", {"return_value": {"helper_version": "0.1.0"}})],
             4, "helper 0.2.0 or later"),
        ]
        for name, args, patches, code, token in cases:
            for as_json in (False, True):
                with self.subTest(case=name, json=as_json):
                    result = self.invoke(args, as_json, patches)
                    self.assertEqual(result.exit_code, code, result.output)
                    if as_json:
                        self.assertEqual(json.loads(result.stdout)["ok"], False)
                    else:
                        self.assertIn(token, result.output)
                        self.assertFalse(looks_like_dump(result.output), result.output)

    def test_invalid_environment_input_is_rejected_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.env"
            path.write_text("")
            patches = [("shiplord.cli._manifest", {"return_value": dict(MANIFEST)}),
                       ("shiplord.cli._admin", {})]
            for as_json in (False, True):
                with self.subTest(json=as_json):
                    result = self.invoke(["env", "set", "--from-file", str(path)], as_json, patches)
                    self.assertEqual(result.exit_code, 2, result.output)
                    self.assertIn("defines no variables", result.output)

    def test_deploy_without_confirmation_explains_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "ship.toml").write_text("placeholder")
            with chdir(directory), patch("shiplord.cli.load_manifest", return_value=dict(MANIFEST)):
                result = self.invoke(["deprovision"], False)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--yes", result.output)


class RendererSafetyTests(unittest.TestCase):
    """The generic renderers are the last line of defence against structure dumps."""

    def render(self, renderer, *args) -> str:
        lines: list[str] = []
        renderer(*args, say=lines.append)
        return "\n".join(lines)

    def test_generic_renderer_never_dumps_a_structure(self):
        text = self.render(output.render_result, {
            "ok": True, "created": {"account": True, "key": False}, "keys": ["A", "B"],
            "nested": [{"a": 1}], "empty": [], "nothing": None,
        })
        self.assertFalse(looks_like_dump(text), text)
        self.assertIn("Outcome: succeeded", text)
        self.assertIn("Created:", text)
        self.assertIn("  account: yes", text)
        self.assertIn("  key: no", text)
        self.assertIn("Keys:\n  - A\n  - B", text)
        self.assertIn("Nested:\n  - see --json", text)
        self.assertIn("Empty:\n  none", text)
        self.assertIn("Nothing: none", text)

    def test_doctor_value_summarises_unlabelled_checks(self):
        self.assertEqual(output._doctor_value({"a": 1, "b": {"c": 2}}), "a: 1; b: see --json")
        self.assertEqual(output._doctor_value({"ok": False, "hint": "create the DNS record"}),
                         "failed (hint: create the DNS record)")
        self.assertEqual(output._doctor_value(True), "available")
        self.assertEqual(output._doctor_value(False), "missing")

    def test_failure_renderer_names_the_message_and_guidance(self):
        text = self.render(output.render_failure, {"ok": False, "error": "no servers are registered",
                                                   "error_code": "invalid_request", "aliases": ["alpha", "beta"]})
        self.assertEqual(text.splitlines(), ["error: no servers are registered", "code: invalid_request",
                                             "registered servers: alpha, beta"])

    def test_job_renderer_explains_recovery(self):
        text = self.render(output.render_job, {"ok": False, "status": "intervention_required", "exit_code": 6,
                                               "error": "recovery_failed", "message": "the app did not exit"},
                           "orders on production", "recover")
        self.assertIn("Outcome: intervention required", text)
        self.assertIn("Reason: recovery failed", text)
        self.assertIn("Message: the app did not exit", text)
        self.assertIn("run `shiplord recover`", text)

    def test_environment_renderer_prints_names_never_values(self):
        text = self.render(output.render_environment, {"ok": True, "keys": ["TOKEN", "URL"], "changed": True},
                           "orders on production", "set")
        self.assertIn("Variables: 2 (TOKEN, URL)", text)
        self.assertNotIn("secret-value", text)

    def test_registry_renderer_prints_usernames_only(self):
        text = self.render(output.render_registry, {"ok": True, "registries": [
            {"registry": "ghcr.io", "username": "bot"}]}, "orders on production", "list")
        self.assertIn("ghcr.io: bot", text)
        self.assertFalse(looks_like_dump(text), text)


if __name__ == "__main__":
    unittest.main()
