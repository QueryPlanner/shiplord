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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .client import AdminClient, ClientError, HelperClient, run_ssh
from .keys import KeyError_, parse_public_key
from .layout import deployment_user
from .manifest import ManifestError, validate_manifest
from .servers import SshOptions, _config_home, ssh_command
from .workflow import TOOLING_REPOSITORY, WORKFLOW_PATH, WorkflowError, render_workflow

SECRET_NAME = "SHIPLORD_SSH_PRIVATE_KEY"
VARIABLES = ("SHIPLORD_SERVER_HOST", "SHIPLORD_DEPLOY_USER", "SHIPLORD_SSH_KNOWN_HOSTS")  # SHIPLORD_SSH_PORT is optional
TAILSCALE_VARIABLES = ("SHIPLORD_TS_CLIENT_ID", "SHIPLORD_TS_AUDIENCE", "SHIPLORD_TS_TAGS")
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

    def last_run(self, repository: str) -> dict | None:
        output = self._run(["run", "list", "--repo", repository, "--workflow", Path(WORKFLOW_PATH).name, "--limit", "1",
                            "--json", "status,conclusion,headSha,url,createdAt"], what="run list").stdout
        runs = json.loads(output or "[]")
        return runs[0] if runs else None


# --- local state -----------------------------------------------------------------

def ci_directory(application_id: str, home: Path | None = None) -> Path:
    base = (home / "shiplord") if home else (_config_home() / "shiplord")
    directory = base / "ci" / application_id
    directory.mkdir(parents=True, exist_ok=True)
    for path in (base, base / "ci", directory):
        os.chmod(path, 0o700)
    return directory


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
                 tailscale: Mapping[str, str] | None = None, force_workflow: bool = False, home: Path | None = None,
                 runner=run_ssh, host_keys: Callable[[str], list[tuple[str, str]]] | None = None,
                 resolve: Callable[[str], tuple[str, int]] = resolve_ssh_host) -> dict:
    if not _REPOSITORY.fullmatch(repository):
        raise GitHubError("--repo must be owner/repository", 2)
    if "github" in manifest and manifest["github"]["repository"] != repository:
        raise GitHubError(f"ship.toml already names {manifest['github']['repository']}; edit it first to change repositories", 2)
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
    directory = ci_directory(app, home)
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

    steps["workflow"] = _write_workflow(manifest, repository, project, state, directory, tooling_ref, platforms, force_workflow)
    _ensure_manifest_github(project, manifest, repository)
    steps["manifest"] = f"ship.toml names {repository}"
    remaining = []
    if not (tailscale and tailscale.get("SHIPLORD_TS_CLIENT_ID")):
        steps["tailscale"] = "not configured"
        remaining.append("Create the Tailscale workload-identity federation for this repository and grant tag:ci access "
                         "to the server's SSH port, then rerun with --tailscale-client-id and --tailscale-audience "
                         "(Shiplord cannot do this without Tailscale administrative credentials).")
    else:
        remaining.append("Make sure the Tailscale trust credential gives tag:ci and that tag:ci may reach the server's "
                         "SSH port; Shiplord cannot check the tailnet policy.")
    if manifest["runtime"] == "docker":
        remaining.append("Give the server a read-only pull credential if the image is private: `shiplord registry login`.")
    remaining.append("No GitHub Actions run has been performed: nothing here claims the pipeline works until one succeeds.")
    return {"ok": True, "repository": repository, "github_account": account, "key_label": state["label"],
            "key_fingerprint": state["fingerprint"], "server_host": server_host, "steps": steps, "remaining": remaining}


def _write_workflow(manifest: dict, repository: str, project: Path, state: dict, directory: Path,
                    tooling_ref: str | None, platforms: str, force: bool) -> str:
    if manifest["runtime"] != "docker":
        return "skipped: generation covers Docker applications; call `shiplord deploy --binary` from your own build job"
    ref = tooling_ref or detect_tooling_ref()
    if ref is None:
        raise GitHubError("cannot pin the Shiplord version for CI: pass --tooling-ref <commit sha> (a commit that is "
                          "pushed to GitHub)", 2, "workflow")
    try:
        text = render_workflow(dict(manifest, github={"repository": repository, **manifest.get("github", {"deployment_branch": "main"})}),
                               tooling_ref=ref, platforms=platforms)
    except WorkflowError as error:
        raise GitHubError(str(error), 2, "workflow") from None
    path = project / WORKFLOW_PATH
    digest = hashlib.sha256(text.encode()).hexdigest()
    if path.exists():
        existing = hashlib.sha256(path.read_bytes()).hexdigest()
        if existing == digest:
            return f"unchanged ({WORKFLOW_PATH})"
        if existing != state.get("workflow_sha256") and not force:
            raise GitHubError(f"{WORKFLOW_PATH} was edited since Shiplord generated it; review it, or pass "
                              "--force-workflow to replace it", 2, "workflow")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text)
    os.replace(temporary, path)
    state["workflow_sha256"] = digest
    _save_state(directory, state)
    return f"written ({WORKFLOW_PATH}, Shiplord pinned at {ref[:12]})"


def _ensure_manifest_github(project: Path, manifest: dict, repository: str) -> None:
    """Add the [github] section to ship.toml if the file (not just the loaded dict) lacks it."""
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
        return
    text += ("" if text.endswith("\n") else "\n") + f'\n[github]\nrepository = "{repository}"\ndeployment_branch = "main"\n'
    try:
        validate_manifest(tomllib.loads(text))
    except (ManifestError, ValueError):
        raise GitHubError("could not add the [github] section to ship.toml", 1, "manifest") from None
    temporary = path.with_name(f".ship.toml.tmp-{os.getpid()}")
    temporary.write_text(text)
    os.replace(temporary, path)


def rotate_key(manifest: dict, *, admin: AdminClient, gh: GitHub, admin_target: str, home: Path | None = None,
               runner=run_ssh, host_keys: Callable[[str], list[tuple[str, str]]] | None = None,
               resolve: Callable[[str], tuple[str, int]] = resolve_ssh_host) -> dict:
    """Replace the CI key: add, prove, store in GitHub, and only then remove the old key."""
    app = manifest["name"]
    directory = ci_directory(app, home)
    state = _load_state(directory)
    if not state.get("secret_set") or not state.get("label") or "github" not in manifest:
        raise GitHubError("no completed CI setup was found for this application; run `shiplord github setup` first", 2)
    repository, old_label = manifest["github"]["repository"], state["label"]
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
    state.update(label=new_label, fingerprint=fingerprint)
    _save_state(directory, state)
    try:
        admin.call("keys", app, {"operation": "remove", "label": old_label})
    except ClientError as error:
        raise GitHubError(f"the new key is live, but the old key {old_label} could not be removed: {error}", error.code,
                          "revoke") from None
    return {"ok": True, "repository": repository, "old_label": old_label, "key_label": new_label, "key_fingerprint": fingerprint,
            "steps": {"register": new_label, "ssh_check": "the new key authenticated", "secret": f"{SECRET_NAME} updated",
                      "revoke": f"{old_label} removed"}}


def github_status(manifest: dict, *, project: Path, gh: GitHub, admin: AdminClient | None = None,
                  home: Path | None = None) -> tuple[int, dict]:
    if "github" not in manifest:
        raise GitHubError("ship.toml has no [github] section; run `shiplord github setup --repo owner/name`", 2)
    app, repository = manifest["name"], manifest["github"]["repository"]
    state = _load_state(ci_directory(app, home))
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
                           "hint": "set SHIPLORD_TS_CLIENT_ID and SHIPLORD_TS_AUDIENCE after creating the federation"}
    workflow = project / WORKFLOW_PATH
    unmodified = workflow.exists() and hashlib.sha256(workflow.read_bytes()).hexdigest() == state.get("workflow_sha256")
    checks["workflow"] = {"ok": workflow.exists(), "unmodified_since_generation": unmodified}
    checks["ci_key"] = {"ok": bool(state.get("secret_set")), "label": state.get("label"), "fingerprint": state.get("fingerprint")}
    if admin is not None:
        listed = {item["label"]: item["fingerprint"] for item in admin.call("keys", app, {"operation": "list"})["keys"]}
        checks["server_key"] = {"ok": state.get("label") in listed and listed.get(state.get("label")) == state.get("fingerprint")}
    run = gh.last_run(repository)
    checks["last_run"] = {"ok": run is not None and run.get("conclusion") == "success", "run": run,
                          "note": None if run else "no workflow run has happened yet"}
    ok = all(check["ok"] for check in checks.values())
    return (0 if ok else 3), {"ok": ok, "repository": repository, "checks": checks}
