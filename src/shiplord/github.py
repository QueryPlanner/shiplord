"""GitHub repository configuration for CI deployments (client-side): setup, status, and key rotation.

The CI key is a dedicated Ed25519 key. It is registered on the server, proven to work over SSH with pinned
host keys, and only then stored in GitHub (as a secret, through stdin). The personal key is never uploaded.
Setup records its progress so a failure part way can be resumed; the private key is kept (0600) only until
GitHub has it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import __version__
from . import tailscale
from .client import AdminClient, ClientError, HelperClient, run_ssh
from .keys import KeyError_, parse_public_key
from .layout import deployment_user
from .manifest import (ManifestError, configured_branch, validate_binary_path, validate_build_command,
                       validate_deployment_branch, validate_manifest)
from .servers import SshOptions, _config_home, ssh_command
from .workflow import (DEFAULT_QUALITY_WORKFLOW, DEPLOY_WORKFLOW_PATH, TOOLING_REPOSITORY, WORKFLOW_PATH, WorkflowError,
                       render_deploy_workflow, render_workflow, upgrade_action_pins, validate_platforms,
                       validate_quality_workflow)

SECRET_NAME = "SHIPLORD_SSH_PRIVATE_KEY"
VARIABLES = ("SHIPLORD_SERVER_HOST", "SHIPLORD_DEPLOY_USER", "SHIPLORD_SSH_KNOWN_HOSTS")  # SHIPLORD_SSH_PORT is optional
TAILSCALE_VARIABLES = ("SHIPLORD_TS_CLIENT_ID", "SHIPLORD_TS_AUDIENCE", "SHIPLORD_TS_TAGS")
TAILSCALE_API_TOKEN_ENV = tailscale.API_TOKEN_ENV  # the github_setup parameter shadows the module name
WRITE_PERMISSIONS = ("ADMIN", "MAINTAIN")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_HOST_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")


class GitHubError(Exception):
    """GitHub or CI setup failed; ``code`` is the CLI exit status and ``step`` names where it stopped."""

    def __init__(self, message: str, code: int = 1, step: str | None = None):
        super().__init__(message)
        self.code, self.step = code, step


@dataclass(frozen=True)
class GhResult:
    code: int
    stdout: str
    stderr: str


Gh = Callable[[list[str], bytes | None], GhResult]


def run_gh(argv: list[str], stdin: bytes | None = None) -> GhResult:
    try:
        result = subprocess.run(["gh", *argv], input=stdin, capture_output=True, timeout=120, check=False)
    except FileNotFoundError:
        raise GitHubError("the GitHub CLI (gh) is not installed", 3) from None
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError("the GitHub CLI did not complete", 3) from None
    return GhResult(result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace"))


class GitHub:
    """The few GitHub operations needed, through the user's authenticated ``gh``. Values go in via stdin."""

    def __init__(self, runner: Gh = run_gh):
        self.runner = runner

    def _run(self, argv: list[str], stdin: bytes | None = None, *, what: str) -> GhResult:
        result = self.runner(argv, stdin)
        if result.code != 0:
            raise GitHubError(f"GitHub {what} failed: {result.stderr.strip()[:200] or 'no details'}", 3)
        return result

    def account(self) -> str:
        return self._run(["api", "user", "--jq", ".login"], what="authentication check").stdout.strip()

    def permission(self, repository: str) -> str:
        output = self._run(["repo", "view", repository, "--json", "viewerPermission"], what="repository check").stdout
        try:
            return json.loads(output)["viewerPermission"]
        except (ValueError, KeyError, TypeError):
            raise GitHubError("GitHub returned an unexpected repository response", 3) from None

    def set_secret(self, repository: str, name: str, value: bytes) -> None:
        self._run(["secret", "set", name, "--repo", repository], value, what=f"secret {name}")

    def set_variable(self, repository: str, name: str, value: str) -> None:
        self._run(["variable", "set", name, "--repo", repository], value.encode(), what=f"variable {name}")

    def secret_names(self, repository: str) -> set[str]:
        output = self._run(["secret", "list", "--repo", repository, "--json", "name"], what="secret list").stdout
        return {item["name"] for item in json.loads(output or "[]")}

    def variables(self, repository: str) -> dict[str, str]:
        output = self._run(["variable", "list", "--repo", repository, "--json", "name,value"], what="variable list").stdout
        return {item["name"]: item["value"] for item in json.loads(output or "[]")}

    def oidc_subject_prefix(self, repository: str) -> tuple[str, list[str]]:
        """GitHub's OIDC subject prefix for the repository and any customized claim keys.

        Repositories created with immutable subjects use ``repo:Owner@<id>/name@<id>`` instead of the names alone.
        """
        result = self.runner(["api", f"repos/{repository}/actions/oidc/customization/sub"], None)
        try:
            data = json.loads(result.stdout) if result.code == 0 else {}
        except ValueError:
            data = {}
        prefix = data.get("sub_claim_prefix") if isinstance(data.get("sub_claim_prefix"), str) else None
        custom = [] if data.get("use_default", True) else [str(key) for key in data.get("include_claim_keys") or []]
        return prefix or f"repo:{repository}", custom

    def dispatch_runs(self, repository: str, branch: str) -> list[dict]:
        output = self._run(["run", "list", "--repo", repository, "--workflow", Path(WORKFLOW_PATH).name, "--event",
                            "workflow_dispatch", "--branch", branch, "--limit", "10", "--json", "databaseId,status,url"],
                           what="run list").stdout
        return json.loads(output or "[]")

    def dispatch_check(self, repository: str, branch: str) -> None:
        result = self.runner(["workflow", "run", Path(WORKFLOW_PATH).name, "--repo", repository, "--ref", branch,
                              "-f", "check_only=true"], None)
        if result.code != 0:
            detail = result.stderr.strip()[:200]
            if "check_only" in detail or "nexpected input" in detail:
                raise GitHubError("the workflow has no check-only mode yet; regenerate it with `shiplord github setup "
                                  "--force-workflow` and push it", 2, "dispatch")
            raise GitHubError(f"GitHub could not start the workflow: {detail or 'no details'}", 3, "dispatch")

    def run_view(self, repository: str, run_id: int) -> dict:
        output = self._run(["run", "view", str(run_id), "--repo", repository, "--json", "status,conclusion,url,jobs"],
                           what="run view").stdout
        return json.loads(output or "{}")

    def last_run(self, repository: str) -> dict | None:
        output = self._run(["run", "list", "--repo", repository, "--workflow", Path(WORKFLOW_PATH).name, "--limit", "1",
                            "--json", "status,conclusion,headSha,url,createdAt"], what="run list").stdout
        runs = json.loads(output or "[]")
        return runs[0] if runs else None


# --- local state -----------------------------------------------------------------

def ci_directory(application_id: str, server: str, home: Path | None = None) -> Path:
    """The private per-server, per-application directory; state from the earlier layout is moved into it."""
    base = (home / "shiplord") if home else (_config_home() / "shiplord")
    root = base / "ci"
    directory = root / server / application_id
    directory.mkdir(parents=True, exist_ok=True)
    for path in (base, root, root / server, directory):
        os.chmod(path, 0o700)
    _migrate_legacy_state(root / application_id, directory)
    return directory


def _ci_base(home: Path | None = None) -> Path:
    return (home / "shiplord") if home else (_config_home() / "shiplord")


def ci_state(application_id: str, server: str, home: Path | None = None) -> dict:
    """The recorded CI setup state for an application, without creating anything."""
    return _load_state(_ci_base(home) / "ci" / server / application_id)


def _migrate_legacy_state(legacy: Path, directory: Path) -> None:
    """Move state written at ``ci/<app>`` into the server-scoped directory; never overwrite newer state.

    A private key retained by an interrupted setup moves with the state, so its recorded path is rewritten.
    Migration is best effort: an unmovable directory costs at most one extra key, never a failed command.
    """
    if legacy == directory or legacy.is_symlink() or not legacy.is_dir() or (directory / "state.json").exists():
        return
    try:
        for entry in sorted(legacy.iterdir()):
            if entry.is_file() and not entry.is_symlink():
                os.replace(entry, directory / entry.name)
        legacy.rmdir()
        state = _load_state(directory)
        private = state.get("private_key")
        if isinstance(private, str) and Path(private).parent == legacy:
            moved = directory / Path(private).name
            if moved.is_file():
                state["private_key"] = str(moved)
                _save_state(directory, state)
    except OSError:
        pass


def _load_state(directory: Path) -> dict:
    try:
        return json.loads((directory / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_state(directory: Path, state: dict) -> None:
    temporary = directory / f".state.tmp-{os.getpid()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(state, file, sort_keys=True)
    os.replace(temporary, directory / "state.json")


def generate_ci_key(directory: Path, application_id: str, name: str) -> tuple[Path, str]:
    """Create a dedicated Ed25519 key pair in the private CI directory; returns (private path, public text)."""
    private = directory / name
    for leftover in (private, private.with_suffix(".pub"), Path(str(private) + ".pub")):
        leftover.unlink(missing_ok=True)
    try:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"shiplord-ci-{application_id}", "-f", str(private)],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise GitHubError("could not generate the CI key (is ssh-keygen installed?)", 3, "key") from None
    os.chmod(private, 0o600)
    return private, Path(str(private) + ".pub").read_text().strip()


def label_for(fingerprint: str) -> str:
    return "ci-" + re.sub(r"[^a-z0-9]", "", fingerprint.split(":", 1)[1].lower())[:8]


def ci_key_labels(listed: list[dict]) -> list[str]:
    """The labels Shiplord derives from a key's own fingerprint; a key whose label does not match is not ours."""
    labels = []
    for item in listed:
        label, fingerprint = item.get("label"), item.get("fingerprint")
        if isinstance(label, str) and isinstance(fingerprint, str) and fingerprint.startswith("SHA256:"):
            if label == label_for(fingerprint):
                labels.append(label)
    return sorted(labels)


# --- host keys ------------------------------------------------------------------------

def parse_host_key(line: str) -> tuple[str, str]:
    fields = line.split()
    if len(fields) < 2 or fields[0] not in _HOST_KEY_TYPES:
        raise GitHubError("the server returned an unusable host key", 3, "host-keys")
    try:
        blob = base64.b64decode(fields[1], validate=True)
        name_length = int.from_bytes(blob[:4], "big")
        if blob[4:4 + name_length].decode("ascii") != fields[0]:
            raise ValueError
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise GitHubError("the server returned a malformed host key", 3, "host-keys") from None
    return fields[0], fields[1]


def known_hosts_lines(host: str, port: int, keys: list[tuple[str, str]]) -> str:
    name = host if port == 22 else f"[{host}]:{port}"
    return "".join(f"{name} {key_type} {blob}\n" for key_type, blob in keys)


def fetch_host_keys(admin_target: str, runner=run_ssh, options: SshOptions | None = None) -> list[tuple[str, str]]:
    """Read the server's public host keys over the already-trusted SSH connection (never from a keyscan)."""
    stdout, _stderr, status = runner(ssh_command(admin_target, "cat /etc/ssh/ssh_host_ed25519_key.pub "
                                                 "/etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub 2>/dev/null",
                                                 options), b"", 30)
    keys = [parse_host_key(line) for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if not keys:
        raise GitHubError("could not read the server's host keys over SSH", 3, "host-keys")
    return keys


def resolve_ssh_host(host: str, config: Path | None = None) -> tuple[str, int]:
    """The real host name and port OpenSSH would use for an alias, from the user's own SSH configuration."""
    if not _HOST.fullmatch(host):
        raise GitHubError("invalid server host", 2)
    try:
        command = ["ssh", *(["-F", str(config)] if config else []), "-G", host]
        output = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return host, 22
    settings = dict(line.split(" ", 1) for line in output.splitlines() if " " in line)
    port = settings.get("port", "22")
    return settings.get("hostname", host), int(port) if port.isdigit() else 22


def detect_tooling_ref() -> str | None:
    """The commit of the Shiplord checkout this CLI runs from, if it is a git checkout."""
    directory = Path(__file__).resolve().parent
    try:
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    ref = result.stdout.strip()
    return ref if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", ref) else None


# --- operations ---------------------------------------------------------------------------

def _native_build_settings(manifest: dict, build_command: str | None,
                           binary_path: str | None) -> tuple[str | None, str | None]:
    """Resolve how CI builds a native binary, refusing before any account, key, or GitHub change.

    The values live in ``[github]`` so a later run needs no flags. A section that predates this feature has no
    place to write them without rewriting the user's file, so it is refused with the two lines to add.
    """
    if manifest["runtime"] != "native":
        if build_command is not None or binary_path is not None:
            raise GitHubError("--build-command and --binary-path apply only to native applications", 2, "configuration")
        return None, None
    section = manifest.get("github") or {}
    if section and not (section.get("build_command") and section.get("binary_path")):
        raise GitHubError("ship.toml already has a [github] section without github.build_command and "
                          "github.binary_path; add both to that section", 2, "configuration")
    resolved: dict[str, str | None] = {}
    for name, given, existing, validate in (
        ("build_command", build_command, section.get("build_command"), validate_build_command),
        ("binary_path", binary_path, section.get("binary_path"), validate_binary_path),
    ):
        if given is not None:
            try:
                given = validate(given)
            except ManifestError as error:
                raise GitHubError(str(error), 2, "configuration") from None
            if existing is not None and existing != given:
                raise GitHubError(f"ship.toml already names github.{name}; edit it first to change it", 2,
                                  "configuration")
        resolved[name] = given if given is not None else existing
    if not resolved["build_command"] or not resolved["binary_path"]:
        raise GitHubError("a native application needs the command that builds its binary and the path it writes: "
                          "pass --build-command and --binary-path, or add github.build_command and "
                          "github.binary_path to ship.toml", 2, "configuration")
    return resolved["build_command"], resolved["binary_path"]


def validate_setup_profile(manifest: dict, *, build_command: str | None = None, binary_path: str | None = None,
                           platforms: str | None = None) -> tuple[str | None, str | None]:
    """Reject an unsupported workflow request before callers collect settings or credentials."""
    if platforms is not None:
        try:
            validate_platforms(manifest["runtime"], platforms)
        except WorkflowError as error:
            raise GitHubError(str(error), 2, "configuration") from None
    return _native_build_settings(manifest, build_command, binary_path)


def validate_setup_request(manifest: dict, repository: str) -> None:
    """Reject an unusable setup request before callers collect settings, credentials, or change anything.

    Nothing here is template-specific: every supported template gets the same CI key, secret, variables and
    generated pipeline. Only the runtime decides whether a workflow is generated.
    """
    if not _REPOSITORY.fullmatch(repository):
        raise GitHubError("--repo must be owner/repository", 2)
    if "github" in manifest and manifest["github"]["repository"] != repository:
        raise GitHubError(f"ship.toml already names {manifest['github']['repository']}; edit it first to change repositories", 2)


def validate_repository(repository: str) -> str:
    """Reject a repository that is not an ``owner/repository`` pair before anything is changed."""
    if not _REPOSITORY.fullmatch(repository):
        raise GitHubError("--repo must be owner/repository", 2)
    return repository


def setup_complete(manifest: dict, *, project: Path, repository: str, home: Path | None = None) -> bool:
    """Local evidence that ``github setup`` finished for this repository.

    It reads the manifest, the generated workflow, and the local CI state; it does not query GitHub, so a secret
    deleted in the repository is not noticed here (``shiplord github status`` and the final check do notice it).
    """
    if manifest.get("github", {}).get("repository") != repository:
        return False
    if not (project / WORKFLOW_PATH).is_file():
        return False
    state = ci_state(manifest["name"], manifest["server"], home)
    return bool(state.get("secret_set")) and state.get("repository") == repository


def resolve_deployment_branch(manifest: dict, requested: str | None = None) -> str:
    """The branch CI deploys from: the requested one, else the manifest's, else main. Validated before any change."""
    try:
        return validate_deployment_branch(requested if requested is not None else configured_branch(manifest))
    except ManifestError as error:
        raise GitHubError(str(error), 2, "configuration") from None


def _verify_ci_access(application_id: str, host: str, port: int, private: Path,
                      keys: list[tuple[str, str]], runner=run_ssh) -> None:
    """Prove the CI key works against the pinned host keys, exactly as the workflow will use it.

    CI ignores ~/.ssh/config, so this connects to the resolved host name, never to the administrator's SSH alias.
    """
    with tempfile.TemporaryDirectory() as temp:
        pinned = Path(temp) / "known_hosts"
        pinned.write_text(known_hosts_lines(host, port, keys))
        client = HelperClient(application_id, f"{deployment_user(application_id)}@{host}", runner=runner,
                              ssh_options=SshOptions(private, pinned, port if port != 22 else None), explicit_target=True)
        try:
            client.call("capabilities")
        except ClientError as error:
            raise GitHubError(f"the CI key does not work against the server: {error}", error.code, "verify") from None


def github_setup(manifest: dict, repository: str, *, project: Path, admin: AdminClient, gh: GitHub, admin_target: str,
                 ci_host: str | None = None, tooling_ref: str | None = None, platforms: str = "linux/amd64",
                 build_command: str | None = None, binary_path: str | None = None,
                 tailscale: Mapping[str, str] | None = None, deployment_branch: str | None = None,
                 force_workflow: bool = False, home: Path | None = None,
                 runner=run_ssh, host_keys: Callable[[str], list[tuple[str, str]]] | None = None,
                 resolve: Callable[[str], tuple[str, int]] = resolve_ssh_host, quality_workflow: str | None = None,
                 include_quality: bool = True) -> dict:
    command, path = validate_setup_profile(manifest, build_command=build_command, binary_path=binary_path,
                                           platforms=platforms)
    if manifest["runtime"] == "native" and quality_workflow is not None:
        raise GitHubError("a native workflow has no quality job; run your checks in the build command", 2,
                          "configuration")
    validate_setup_request(manifest, repository)
    branch = resolve_deployment_branch(manifest, deployment_branch)
    app = manifest["name"]
    steps: dict[str, str] = {}
    account = gh.account()
    permission = gh.permission(repository)
    if permission not in WRITE_PERMISSIONS:
        raise GitHubError(f"your GitHub account {account} has {permission} access to {repository}; setting Actions "
                          "secrets needs ADMIN or MAINTAIN", 3, "access")
    steps["github_access"] = f"{account} ({permission})"

    host, port = resolve(admin_target.partition("@")[2])
    server_host = ci_host or host
    keys = (host_keys or (lambda target: fetch_host_keys(target, runner)))(admin_target)
    directory = ci_directory(app, manifest["server"], home)
    state = _load_state(directory)
    reuse = bool(state.get("fingerprint")) and state.get("repository") == repository and state.get("secret_set")
    private: Path | None = None
    if reuse:
        steps["ci_key"] = f"reused {state['label']}"
    else:
        if state.get("private_key") and Path(state["private_key"]).is_file() and state.get("repository") == repository:
            private = Path(state["private_key"])  # resuming after a partial failure
            public = Path(str(private) + ".pub").read_text().strip()
        else:
            private, public = generate_ci_key(directory, app, f"key-{os.getpid()}")
        try:
            fingerprint = parse_public_key(public).fingerprint
        except KeyError_:
            raise GitHubError("the generated CI key is unusable", 1, "key") from None
        state.update(repository=repository, label=label_for(fingerprint), fingerprint=fingerprint, private_key=str(private),
                     secret_set=False)
        _save_state(directory, state)
        try:
            admin.call("keys", app, {"operation": "add", "label": state["label"], "public_key": public})
        except ClientError as error:
            raise GitHubError(f"could not register the CI key on the server: {error}", error.code, "register") from None
        steps["ci_key"] = f"registered {state['label']}"
        _verify_ci_access(app, server_host, port, private, keys, runner)
        steps["ssh_check"] = "the CI key authenticated against the pinned host keys"
        gh.set_secret(repository, SECRET_NAME, private.read_bytes())
        state["secret_set"] = True
        _save_state(directory, state)
        for leftover in (private, Path(str(private) + ".pub")):
            leftover.unlink(missing_ok=True)
        state["private_key"] = None
        _save_state(directory, state)
        steps["secret"] = f"{SECRET_NAME} stored; the local private key was removed"

    variables = {"SHIPLORD_SERVER_HOST": server_host, "SHIPLORD_DEPLOY_USER": deployment_user(app),
                 "SHIPLORD_SSH_KNOWN_HOSTS": known_hosts_lines(server_host, port, keys), **(tailscale or {})}
    if port != 22:
        variables["SHIPLORD_SSH_PORT"] = str(port)
    for name, value in variables.items():
        gh.set_variable(repository, name, value)
    steps["variables"] = ", ".join(sorted(variables))

    steps["workflow"] = _write_workflow(manifest, repository, project, state, directory, tooling_ref, platforms, branch,
                                        force_workflow, quality_workflow, include_quality, command, path)
    _ensure_manifest_github(project, repository, branch, command, path)
    steps["manifest"] = f"ship.toml names {repository} on {branch}"
    remaining = []
    extra: dict = {}
    if not (tailscale and tailscale.get("SHIPLORD_TS_CLIENT_ID")):
        steps["tailscale"] = "not configured"
        extra["tailscale_setup"] = tailscale_guidance(gh, repository, manifest, branch)
        remaining.append("Create the Tailscale trust credential as described in tailscale_setup, then rerun with "
                         "--tailscale-client-id and --tailscale-audience. Shiplord creates it for you when "
                         f"{TAILSCALE_API_TOKEN_ENV} holds a Tailscale API token that may create federated identities.")
    else:
        remaining.append("Make sure the Tailscale trust credential gives tag:ci and that tag:ci may reach the server's "
                         "SSH port; Shiplord cannot check the tailnet policy.")
    if manifest["runtime"] == "docker":
        remaining.append("Give the server a read-only pull credential if the image is private: `shiplord registry login`.")
    remaining.append("No GitHub Actions run has been performed: nothing here claims the pipeline works until one succeeds.")
    return {"ok": True, "repository": repository, "github_account": account, "key_label": state["label"],
            "key_fingerprint": state["fingerprint"], "server_host": server_host, "steps": steps, "remaining": remaining,
            **extra}


def tailscale_guidance(gh: GitHub, repository: str, manifest: dict, branch: str | None = None) -> dict:
    """Instructions for the Tailscale trust credential, with the exact subject this repository's runs present."""
    branch = branch if branch is not None else configured_branch(manifest)
    prefix, custom = gh.oidc_subject_prefix(repository)
    try:
        return tailscale.guidance(prefix, branch, custom_claims=custom)
    except tailscale.TailscaleError as error:
        raise GitHubError(str(error), 3, "tailscale") from None


def tailscale_credential(gh: GitHub, repository: str, manifest: dict, *, tags: list[str] | None = None,
                         environ: Mapping[str, str] | None = None, transport: tailscale.Transport | None = None
                         ) -> tuple[dict[str, str], list[str]] | None:
    """Create the trust credential through the Tailscale API when a token is in the environment.

    Returns ``(settings, warnings)`` or ``None`` when no token is set, so the caller falls back to the printed
    instructions. The owner-wide subject is used, so a later repository of the same owner reuses this credential
    instead of needing its own manual step. The token is never returned, stored or printed.
    """
    token = tailscale.api_token(environ)
    if token is None:
        return None
    instructions = tailscale_guidance(gh, repository, manifest)
    return tailscale.create_federated_identity(
        token, subject=instructions["subject_all_repositories"], tags=tags or [instructions["tag"]],
        description=f"shiplord: {repository}", transport=transport)


def _resolve_quality_workflow(project: Path, requested: str | None, include: bool) -> str | None:
    """The reusable quality workflow to run before deploy, or ``None``.

    Never guessed by the renderer: an explicit request wins, otherwise the conventional file is used only
    when the project really contains it, so a generated workflow never references a file the user lacks.
    """
    if not include:
        return None
    if requested is not None:
        try:
            return validate_quality_workflow(requested)
        except WorkflowError as error:
            raise GitHubError(str(error), 2, "workflow") from None
    return DEFAULT_QUALITY_WORKFLOW if (project / DEFAULT_QUALITY_WORKFLOW).is_file() else None


def _write_workflow(manifest: dict, repository: str, project: Path, state: dict, directory: Path,
                    tooling_ref: str | None, platforms: str, branch: str, force: bool,
                    quality_workflow: str | None = None, include_quality: bool = True,
                    build_command: str | None = None, binary_path: str | None = None) -> str:
    ref = tooling_ref or detect_tooling_ref()
    version = None
    if ref is None:
        # A PyPI install has no git checkout to pin, so pin the installed release instead.
        version = __version__
    pin = version if version is not None else ref[:12]
    merged = dict(manifest, github={"repository": repository, "deployment_branch": branch})
    try:
        if manifest["runtime"] == "native":
            # A native binary needs one file: the build job and the deploy job run in the same workflow.
            merged["github"] = {**merged["github"], "build_command": build_command, "binary_path": binary_path}
            texts = {WORKFLOW_PATH: render_workflow(merged, tooling_ref=ref, tooling_version=version,
                                                    platforms=platforms)}
        else:
            quality = _resolve_quality_workflow(project, quality_workflow, include_quality)
            texts = {DEPLOY_WORKFLOW_PATH: render_deploy_workflow(merged, tooling_ref=ref, tooling_version=version,
                                                                  platforms=platforms),
                     WORKFLOW_PATH: render_workflow(merged, tooling_ref=ref, tooling_version=version,
                                                    platforms=platforms, quality_workflow=quality)}
    except WorkflowError as error:
        raise GitHubError(str(error), 2, "workflow") from None
    digests = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in texts.items()}
    recorded = state.get("workflows") if isinstance(state.get("workflows"), dict) else {}
    # Check every file before writing any, so a refusal never leaves half a pipeline behind.
    changed = []
    for path in texts:
        target = project / path
        if target.exists():
            existing = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing == digests[path]:
                continue
            previous = recorded.get(path) or (state.get("workflow_sha256") if path == WORKFLOW_PATH else None)
            if existing != previous and not force:
                raise GitHubError(f"{path} was edited since Shiplord generated it; review it, or pass "
                                  "--force-workflow to replace it", 2, "workflow")
        changed.append(path)
    for path in changed:
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        temporary.write_text(texts[path])
        os.replace(temporary, target)
    state["workflows"] = {**recorded, **digests}
    state["workflow_sha256"] = digests[WORKFLOW_PATH]  # kept so an older installation's record still matches
    _save_state(directory, state)
    if not changed:
        return f"unchanged ({', '.join(texts)})"
    return f"written ({', '.join(changed)}, Shiplord pinned at {pin})"


_GITHUB_HEADER = re.compile(r"^[ \t]*\[github\][ \t]*(?:#.*)?$", re.MULTILINE)
_TABLE_HEADER = re.compile(r"^[ \t]*\[", re.MULTILINE)
_BRANCH_ASSIGNMENT = re.compile(r"^([ \t]*)deployment_branch[ \t]*=[^\n#]*(?P<comment>#[^\n]*)?$", re.MULTILINE)


def _record_branch(text: str, branch: str) -> str:
    """Set ``deployment_branch`` inside the existing [github] table, leaving every other line untouched."""
    header = _GITHUB_HEADER.search(text)
    if header is None:
        raise GitHubError("ship.toml must declare [github] as a table to record the deployment branch", 2, "manifest")
    following = _TABLE_HEADER.search(text, header.end())
    end = following.start() if following else len(text)
    table = text[header.end():end]
    assignment = f'deployment_branch = "{branch}"'

    def replace(match: re.Match) -> str:
        comment = f"  {match.group('comment')}" if match.group("comment") else ""
        return match.group(1) + assignment + comment

    if _BRANCH_ASSIGNMENT.search(table):
        table = _BRANCH_ASSIGNMENT.sub(replace, table, count=1)
    else:
        table = "\n" + assignment + table
    return text[:header.end()] + table + text[end:]


def _ensure_manifest_github(project: Path, repository: str, branch: str, build_command: str | None = None,
                            binary_path: str | None = None) -> None:
    """Record the repository, deployment branch, and native build settings, adding [github] if it is missing."""
    import tomllib
    path = project / "ship.toml"
    text = path.read_text()
    try:
        existing = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise GitHubError("ship.toml is not valid TOML", 2, "manifest") from None
    if "github" in existing:
        if existing["github"].get("repository") != repository:
            raise GitHubError(f"ship.toml names {existing['github'].get('repository')}, not {repository}", 2, "manifest")
        if existing["github"].get("deployment_branch") == branch:
            return
        text = _record_branch(text, branch)
    else:
        lines = [f'repository = "{repository}"', f'deployment_branch = "{branch}"']
        if build_command is not None:
            lines += [f"build_command = {json.dumps(build_command)}", f"binary_path = {json.dumps(binary_path)}"]
        text += ("" if text.endswith("\n") else "\n") + "\n[github]\n" + "\n".join(lines) + "\n"
    try:
        recorded = validate_manifest(tomllib.loads(text))
    except (ManifestError, ValueError):
        raise GitHubError("could not update the [github] section in ship.toml", 1, "manifest") from None
    if recorded["github"]["deployment_branch"] != branch:
        raise GitHubError("could not update the [github] section in ship.toml", 1, "manifest")
    temporary = path.with_name(f".ship.toml.tmp-{os.getpid()}")
    temporary.write_text(text)
    os.replace(temporary, path)


def upgrade_workflow(manifest: dict, *, project: Path, home: Path | None = None) -> dict:
    """Refresh the action pins in the workflows Shiplord generated, and change nothing else.

    Every file is read and accepted before any is written, so a refusal leaves the pipeline exactly as it was.
    Only ``uses:`` lines for actions Shiplord pins are rewritten: the deployment branch, the pinned Shiplord
    commit, the quality job, and every local edit survive byte for byte. A hand-written workflow is refused.
    """
    paths = (WORKFLOW_PATH,) if manifest["runtime"] == "native" else (DEPLOY_WORKFLOW_PATH, WORKFLOW_PATH)
    missing = [path for path in paths if not (project / path).is_file()]
    if missing:
        raise GitHubError(f"there is no generated workflow at {', '.join(missing)}; run `shiplord github setup` "
                          "to write the pipeline first", 2, "workflow")
    before: dict[str, bytes] = {}
    refreshed: dict[str, str] = {}
    changes: list[dict] = []
    for path in paths:
        try:
            before[path] = (project / path).read_bytes()
            text, found = upgrade_action_pins(before[path].decode("utf-8"))
        except (OSError, UnicodeDecodeError):
            raise GitHubError(f"could not read {path}", 1, "workflow") from None
        except WorkflowError as error:
            raise GitHubError(f"{path} was not rewritten: {error}", 2, "workflow") from None
        refreshed[path] = text
        changes.extend({"path": path, "action": change.repository, "from": change.previous, "to": change.commit,
                        "version": change.version} for change in found)
    written = [path for path in paths if refreshed[path].encode() != before[path]]
    if not written:
        return {"ok": True, "changed": [], "unchanged": list(paths), "pins": [],
                "note": "the generated workflows already pin the current action commits"}
    directory = ci_directory(manifest["name"], manifest["server"], home)
    state = _load_state(directory)
    recorded = state.get("workflows") if isinstance(state.get("workflows"), dict) else {}
    previous = {path: hashlib.sha256(before[path]).hexdigest() for path in paths}
    digests = {}
    for path in paths:
        target = project / path
        if path in written:
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            temporary.write_text(refreshed[path])
            os.replace(temporary, target)
        digests[path] = hashlib.sha256((project / path).read_bytes()).hexdigest()
    # Keep the "what Shiplord last wrote" record honest: a file the user had edited is still edited, so a
    # later `github setup` keeps refusing to overwrite it without --force-workflow.
    updated = dict(recorded)
    for path in paths:
        last_written = recorded.get(path) or (state.get("workflow_sha256") if path == WORKFLOW_PATH else None)
        if last_written == previous[path]:
            updated[path] = digests[path]
    state["workflows"] = updated
    if state.get("workflow_sha256") == previous[WORKFLOW_PATH]:
        state["workflow_sha256"] = digests[WORKFLOW_PATH]
    result = {"ok": True, "changed": written, "unchanged": [path for path in paths if path not in written],
              "pins": changes}
    try:
        _save_state(directory, state)
    except OSError:
        result["warning"] = ("the workflows were refreshed, but the local record was not updated; a later "
                             "`github setup` may ask for --force-workflow")
    return result


def rotate_key(manifest: dict, *, admin: AdminClient, gh: GitHub, admin_target: str, home: Path | None = None,
               runner=run_ssh, host_keys: Callable[[str], list[tuple[str, str]]] | None = None,
               resolve: Callable[[str], tuple[str, int]] = resolve_ssh_host) -> dict:
    """Replace the CI key: add, prove, store in GitHub, and only then remove the old key.

    The key to replace is found in the server's authorized-keys list, so rotation works from a machine that never
    ran setup. Only labels Shiplord derives from a key's own fingerprint are ever removed.
    """
    app = manifest["name"]
    if "github" not in manifest:
        raise GitHubError("ship.toml has no [github] section; run `shiplord github setup --repo owner/name`", 2)
    repository = manifest["github"]["repository"]
    old_labels = ci_key_labels(admin.call("keys", app, {"operation": "list"})["keys"])
    if not old_labels:
        raise GitHubError("no CI key is registered on the server for this application; run "
                          "`shiplord github setup` first", 2)
    directory = ci_directory(app, manifest["server"], home)
    state = _load_state(directory)
    host, port = resolve(admin_target.partition("@")[2])
    keys = (host_keys or (lambda target: fetch_host_keys(target, runner)))(admin_target)
    private, public = generate_ci_key(directory, app, f"rotate-{os.getpid()}")
    fingerprint = parse_public_key(public).fingerprint
    new_label = label_for(fingerprint)
    try:
        admin.call("keys", app, {"operation": "add", "label": new_label, "public_key": public})
    except ClientError as error:
        private.unlink(missing_ok=True)
        raise GitHubError(f"could not register the new CI key: {error}", error.code, "register") from None
    try:
        _verify_ci_access(app, host, port, private, keys, runner)
        gh.set_secret(repository, SECRET_NAME, private.read_bytes())
    except GitHubError:
        try:  # the old key stays in place and in GitHub; do not leave the unused new one registered
            admin.call("keys", app, {"operation": "remove", "label": new_label})
        except ClientError:
            pass
        raise
    finally:
        for leftover in (private, Path(str(private) + ".pub")):
            leftover.unlink(missing_ok=True)
    state.update(repository=repository, label=new_label, fingerprint=fingerprint, secret_set=True)
    try:  # a failed cache write must not stop the revocation that keeps one CI key registered
        _save_state(directory, state)
    except OSError:
        pass
    _revoke_keys(admin, app, old_labels)
    return {"ok": True, "repository": repository, "old_label": old_labels[0] if len(old_labels) == 1 else None,
            "old_labels": old_labels, "key_label": new_label, "key_fingerprint": fingerprint,
            "steps": {"register": new_label, "ssh_check": "the new key authenticated", "secret": f"{SECRET_NAME} updated",
                      "revoke": f"{', '.join(old_labels)} removed"}}


def _revoke_keys(admin: AdminClient, application_id: str, labels: list[str]) -> None:
    """Remove every stale CI key; a failure is reported with the new key already live and stored."""
    failures: list[tuple[str, ClientError]] = []
    for label in labels:
        try:
            admin.call("keys", application_id, {"operation": "remove", "label": label})
        except ClientError as error:
            failures.append((label, error))
    if not failures:
        return
    if len(failures) == 1:
        label, error = failures[0]
        raise GitHubError(f"the new key is live, but the old key {label} could not be removed: {error}", error.code,
                          "revoke") from None
    raise GitHubError(f"the new key is live, but these old keys could not be removed: "
                      f"{', '.join(label for label, _ in failures)}", failures[0][1].code, "revoke") from None


def github_status(manifest: dict, *, project: Path, gh: GitHub, admin: AdminClient | None = None,
                  home: Path | None = None) -> tuple[int, dict]:
    if "github" not in manifest:
        raise GitHubError("ship.toml has no [github] section; run `shiplord github setup --repo owner/name`", 2)
    app, repository = manifest["name"], manifest["github"]["repository"]
    state = _load_state(ci_directory(app, manifest["server"], home))
    checks: dict[str, dict] = {}
    permission = gh.permission(repository)
    checks["access"] = {"ok": permission in WRITE_PERMISSIONS, "account": gh.account(), "permission": permission}
    checks["secret"] = {"ok": SECRET_NAME in gh.secret_names(repository), "name": SECRET_NAME}
    variables = gh.variables(repository)
    missing = [name for name in VARIABLES if not variables.get(name)]
    checks["variables"] = {"ok": not missing, "missing": missing,
                           "deploy_user_matches": variables.get("SHIPLORD_DEPLOY_USER") == deployment_user(app)}
    checks["variables"]["ok"] = checks["variables"]["ok"] and checks["variables"]["deploy_user_matches"]
    checks["tailscale"] = {"ok": all(variables.get(name) for name in TAILSCALE_VARIABLES[:2]),
                           "hint": "rerun `shiplord github setup` for step-by-step Tailscale instructions with this "
                                   "repository's exact OIDC subject"}
    workflow_paths = (WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH)
    recorded = state.get("workflows") if isinstance(state.get("workflows"), dict) else {}
    present = [path for path in workflow_paths if (project / path).is_file()]
    missing = [path for path in workflow_paths if path not in present]

    def unmodified(path: str) -> bool:
        previous = recorded.get(path) or (state.get("workflow_sha256") if path == WORKFLOW_PATH else None)
        return bool(previous) and hashlib.sha256((project / path).read_bytes()).hexdigest() == previous

    checks["workflow"] = {"ok": not missing, "missing": missing,
                          "unmodified_since_generation": bool(present) and all(unmodified(path) for path in present)}
    checks["ci_key"] = {"ok": bool(state.get("secret_set")), "label": state.get("label"), "fingerprint": state.get("fingerprint")}
    if admin is not None:
        listed = {item["label"]: item["fingerprint"] for item in admin.call("keys", app, {"operation": "list"})["keys"]}
        checks["server_key"] = {"ok": state.get("label") in listed and listed.get(state.get("label")) == state.get("fingerprint")}
    run = gh.last_run(repository)
    checks["last_run"] = {"ok": run is not None and run.get("conclusion") == "success", "run": run,
                          "note": None if run else "no workflow run has happened yet"}
    ok = all(check["ok"] for check in checks.values())
    return (0 if ok else 3), {"ok": ok, "repository": repository, "checks": checks}


CHECK_HINTS = {
    "Install the pinned Shiplord": "the runner could not install Shiplord at the pinned commit; check that it is pushed",
    "Join the tailnet": "Tailscale refused the runner, or the server did not answer its ping: compare the trust "
                        "credential's subject with subject_expected, and check tag:ci in the tailnet policy",
    "Prepare the SSH identity and pinned host keys": "the CI secret or variables are missing; run `shiplord github setup`",
    "Check access only": "the CI key, host keys or helper were refused; run `shiplord github status --check-server`",
}


def _job(jobs: list[dict], name: str) -> dict:
    """A job by name. A reusable workflow reports its jobs as ``caller / called``."""
    return next((job for job in jobs if str(job.get("name", "")).split(" / ")[-1] == name), {})


def check_ci(manifest: dict, *, gh: GitHub, timeout: float = 1200, clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep) -> tuple[int, dict]:
    """Start a check-only run of the release workflow and report each step, without building or deploying."""
    if "github" not in manifest:
        raise GitHubError("ship.toml has no [github] section; run `shiplord github setup --repo owner/name`", 2)
    repository = manifest["github"]["repository"]
    branch = configured_branch(manifest)
    before = {run["databaseId"] for run in gh.dispatch_runs(repository, branch)}
    gh.dispatch_check(repository, branch)
    deadline = clock() + timeout
    run_id = None
    while run_id is None:
        run_id = next((run["databaseId"] for run in gh.dispatch_runs(repository, branch) if run["databaseId"] not in before),
                      None)
        if run_id is None:
            if clock() >= deadline:
                raise GitHubError("GitHub did not start the check run in time", 1, "dispatch")
            sleep(3)
    while True:
        run = gh.run_view(repository, run_id)
        if run.get("status") == "completed":
            break
        if clock() >= deadline:
            return 1, {"ok": False, "run": run.get("url"), "status": run.get("status"),
                       "error": "the check run did not finish in time; it is still running on GitHub"}
        sleep(10)
    deploy = _job(run.get("jobs") or [], "deploy")
    steps = {step["name"]: step.get("conclusion") for step in deploy.get("steps") or []}
    failed = next((name for name, conclusion in steps.items() if conclusion == "failure"), None)
    ok = run.get("conclusion") == "success" and steps.get("Check access only") == "success"
    result = {"ok": ok, "run": run.get("url"), "conclusion": run.get("conclusion"), "steps": steps}
    if not ok:
        result["failed_step"] = failed
        result["hint"] = CHECK_HINTS.get(failed or "", "read the run log on GitHub")
        if failed == "Join the tailnet":
            result["subject_expected"] = tailscale_guidance(gh, repository, manifest)["subject_repository"]
    return (0 if ok else 1), result
