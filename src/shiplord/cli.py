"""Command-line entry point for the first local milestone."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import typer

from . import __version__
from . import commands, github, tailscale
from .client import AdminClient, ClientError, HelperClient, bootstrap_server
from .commands import CommandError
from .configure import ConfigurationError, create_manifest
from .manifest import ManifestError, load_manifest
from .protocol import IncompatibleProtocol
from .servers import (InvalidTarget, ServerError, SshOptions, SSHCheckError, list_servers, load_server, register_server,
                      validate_target)
from .template import TemplateError, create_project
from .transport import TransportError, probe_capabilities

app = typer.Typer(no_args_is_help=True, help="Create, provision, and ship applications to a server.")
server_app = typer.Typer(no_args_is_help=True, help="Manage servers.")
github_app = typer.Typer(no_args_is_help=True, help="Configure GitHub Actions deployments.")
env_app = typer.Typer(no_args_is_help=True, help="Manage an application's runtime environment on the server.")
app.add_typer(env_app, name="env")
registry_app = typer.Typer(no_args_is_help=True, help="Manage the server's read-only pull login for private images.")
app.add_typer(registry_app, name="registry")
app.add_typer(server_app, name="server")
app.add_typer(github_app, name="github")


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        for key, value in result.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)
            typer.echo(f"{key}: {value}")


def _unavailable(operation: str, as_json: bool = False) -> None:
    _emit({"ok": False, "operation": operation, "error": "not implemented; no remote action was performed"}, as_json)
    raise typer.Exit(1)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"shiplord {__version__}")
        raise typer.Exit()


@app.callback()
def root(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version.")) -> None:
    """Shiplord CLI."""


@app.command()
def plan(image: Optional[str] = typer.Option(None, "--image", help="Ask the server what deploying this image would do."),
         binary: Optional[Path] = typer.Option(None, "--binary", help="Ask what deploying this binary would do (not uploaded)."),
         commit: Optional[str] = typer.Option(None, "--commit"), sequence: Optional[int] = typer.Option(None, "--sequence"),
         allow_older: bool = typer.Option(False, "--allow-older"),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Preview changes: locally, or with --image, what the server would do (read-only)."""
    manifest = _manifest(json_output)
    if image is None and binary is None:
        _emit({"ok": True, "scope": "local configuration preview",
               "remote_changes": "unknown; pass --image or --binary to ask the server", "configuration": manifest}, json_output)
        return

    def action() -> tuple[int, dict]:
        sha = commit or _head_commit()
        commands.validate_deploy_inputs(manifest, binary if binary is not None else image, sha)
        return commands.plan_application(manifest, _helper(manifest), image=image, binary=binary, commit=sha,
                                         sequence=commands.commit_sequence(sha, sequence), allow_older=allow_older)
    _execute(action, json_output)


@app.command()
def doctor(local: bool = typer.Option(False, "--local", help="Check local prerequisites."),
           server: Optional[str] = typer.Option(None, "--server", help="Server alias."),
           deep: bool = typer.Option(False, "--deep", help="With --server: also check Caddy, sshd, Docker, disk, and applications (sudo)."),
           json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Check local prerequisites or a server's protocol capabilities."""
    if local and server:
        _emit({"ok": False, "error": "choose either --local or --server"}, json_output)
        raise typer.Exit(2)
    if server and deep:
        _execute(lambda: commands.server_doctor(AdminClient(load_server(server))), json_output)
        return
    if server:
        try:
            response = probe_capabilities(load_server(server))
        except ManifestError as exc:
            _emit({"ok": False, "error": str(exc)}, json_output)
            raise typer.Exit(2) from None
        except IncompatibleProtocol:
            _emit({"ok": False, "error": "incompatible helper protocol version"}, json_output)
            raise typer.Exit(4) from None
        except ServerError:
            _emit({"ok": False, "error": "server capabilities check failed; verify the alias, SSH trust, login, and dispatcher"}, json_output)
            raise typer.Exit(3) from None
        except TransportError as exc:
            _emit({"ok": False, "error": str(exc)}, json_output)
            raise typer.Exit(3) from None
        _emit({"ok": True, "scope": "protocol capabilities only; server readiness and deployment access were not checked",
               "helper_version": response["helper_version"]}, json_output)
        return
    if not local:
        manifest = _manifest(json_output)
        _execute(lambda: commands.application_doctor(manifest, _helper(manifest)), json_output)
        return
    checks = {name: bool(shutil.which(name)) for name in ("ssh", "git", "gh")}
    ok = all(checks.values())
    _emit({"ok": ok, "scope": "local prerequisites only; server readiness was not checked", "checks": checks}, json_output)
    if not ok:
        raise typer.Exit(3)


@server_app.command("add")
def server_add(alias: str, ssh: str = typer.Option(..., "--ssh")) -> None:
    """Register a server using an already trusted OpenSSH login."""
    try:
        register_server(alias, ssh)
    except (ManifestError, ServerError) as exc:
        typer.echo(f"error: {exc}", err=True)
        code = 2 if isinstance(exc, (ManifestError, InvalidTarget)) else 3 if isinstance(exc, SSHCheckError) else 1
        raise typer.Exit(code) from None
    typer.echo("Server alias saved. Run `shiplord server bootstrap` next. CI host-key setup is not implemented yet.")


@server_app.command("list")
def server_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the registered servers (local only; nothing is contacted)."""
    try:
        servers, invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)
    if json_output:
        _emit({"ok": True, "servers": servers, "invalid_entries": invalid}, True)
        return
    if not servers:
        typer.echo("No servers registered. Add one with `shiplord server add <alias> --ssh <user>@<host>`.")
    width = max((len(server["alias"]) for server in servers), default=0)
    for server in servers:
        typer.echo(f"{server['alias'].ljust(width)}  {server['ssh']}")
    if invalid:
        typer.echo(f"{invalid} invalid {'entry' if invalid == 1 else 'entries'} ignored in the server directory.")


def _fail(message: str, code: int, json_output: bool = False) -> None:
    _emit({"ok": False, "error": message}, json_output)
    raise typer.Exit(code)


def _manifest(json_output: bool = False) -> dict:
    try:
        return load_manifest()
    except ManifestError as error:
        _fail(str(error), 2, json_output)


def _helper(manifest: dict) -> HelperClient:
    """The application's client. CI sets SHIPLORD_SSH_TARGET (the deployment account) and pinned SSH files."""
    options = SshOptions.from_environment(os.environ)
    target = os.environ.get("SHIPLORD_SSH_TARGET")
    if target:
        return HelperClient(manifest["name"], validate_target(target), ssh_options=options, explicit_target=True)
    return HelperClient(manifest["name"], load_server(manifest["server"]), ssh_options=options)


def _admin(manifest: dict) -> AdminClient:
    return AdminClient(load_server(manifest["server"]))


def _progress(json_output: bool) -> Callable[[str], None]:
    return (lambda phase: None) if json_output else (lambda phase: typer.echo(f"  ... {phase}", err=True))


def _execute(action: Callable[[], tuple[int, dict]], json_output: bool) -> None:
    """Run a command function, print its result, and exit with its code; map known errors to exit codes."""
    try:
        code, result = action()
    except (CommandError, ClientError, github.GitHubError) as error:
        _fail(str(error), error.code, json_output)
    except (ManifestError, ServerError) as error:
        _fail(str(error), 2 if isinstance(error, (ManifestError, InvalidTarget)) else 3, json_output)
    _emit(result, json_output)
    if code:
        raise typer.Exit(code)


@server_app.command("bootstrap")
def server_bootstrap(alias: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Install or update the helper on a registered server (asks for the sudo password if needed)."""
    try:
        result = bootstrap_server(load_server(alias), Path(__file__).resolve().parent)
    except (ClientError, ServerError, ManifestError) as error:
        _fail(str(error), getattr(error, "code", 3), json_output)
    _emit({"ok": True, **result}, json_output)


@app.command("init")
def init(name: str, template: str = typer.Option(..., "--template")) -> None:
    """Create an application from the pinned template snapshot."""
    try:
        create_project(Path.cwd(), name, template)
    except (ManifestError, TemplateError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1 if isinstance(exc, TemplateError) else 2) from None
    typer.echo(f"Created {name} from the pinned google-adk template.")


@app.command()
def configure(server: str = typer.Option(..., "--server"), api_domain: str = typer.Option(..., "--api-domain"),
              frontend: Optional[list[str]] = typer.Option(None, "--frontend"),
              no_frontend: bool = typer.Option(False, "--no-frontend"),
              name: Optional[str] = typer.Option(None, "--name"),
              runtime: str = typer.Option("docker", "--runtime"),
              template: str = typer.Option("google-adk", "--template")) -> None:
    """Create ship.toml in the current directory without overwriting it."""
    if bool(frontend) == no_frontend:
        typer.echo("error: specify --frontend or explicitly choose --no-frontend", err=True)
        raise typer.Exit(2)
    try:
        create_manifest(Path.cwd(), name or Path.cwd().name, server, api_domain, frontend or [], runtime, template)
    except (ConfigurationError, ManifestError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo("Created ship.toml. The server alias has not been checked.")


@app.command()
def provision(public_key: Optional[Path] = typer.Option(None, "--public-key", help="Public key to register."),
              image_repository: Optional[str] = typer.Option(None, "--image-repository"),
              max_memory_mb: Optional[int] = typer.Option(None, "--max-memory-mb", help="Ceiling for memory_mb (default 4096)."),
              json_output: bool = typer.Option(False, "--json")) -> None:
    """Create the application's account, route-ready port, state, and key access on the server."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.provision_application(manifest, _admin(manifest), public_key=public_key,
                                                    image_repository=image_repository,
                                                    max_memory_mb=max_memory_mb), json_output)


def _head_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CommandError("could not run git; pass --commit", 3) from None
    if result.returncode != 0:
        raise CommandError("could not read HEAD; pass --commit", 2)
    return result.stdout.strip()


@app.command()
def deploy(image: Optional[str] = typer.Option(None, "--image", help="Immutable image: registry/name@sha256:<digest>."),
           binary: Optional[Path] = typer.Option(None, "--binary"), commit: Optional[str] = typer.Option(None, "--commit"),
           sequence: Optional[int] = typer.Option(None, "--sequence", help="Release ordering key; default: the commit time."),
           request_id: Optional[str] = typer.Option(None, "--request-id", help="Reuse to retry a deployment safely."),
           allow_older: bool = typer.Option(False, "--allow-older"), timeout: Optional[int] = typer.Option(None, "--timeout"),
           require_public: bool = typer.Option(False, "--require-public", help="Fail if the public HTTPS check fails."),
           verify_public: bool = typer.Option(True, "--verify-public/--no-verify-public"),
           json_output: bool = typer.Option(False, "--json")) -> None:
    """Deploy an already built artifact; nothing is built or pushed."""
    if (image is None) == (binary is None):
        _fail("specify exactly one of --image and --binary", 2, json_output)
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        sha = commit or _head_commit()
        commands.validate_deploy_inputs(manifest, binary if binary is not None else image, sha)
        return commands.deploy_application(
            manifest, _helper(manifest), image=image, binary=binary, commit=sha,
            sequence=commands.commit_sequence(sha, sequence),
            request_id=request_id, timeout=timeout, allow_older=allow_older, on_progress=_progress(json_output),
            verify_public=verify_public, require_public=require_public)
    _execute(action, json_output)


@app.command()
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the deployed release, the previous release, and any unfinished transaction."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.application_status(_helper(manifest)), json_output)


@app.command()
def logs(tail: int = typer.Option(100, "--tail")) -> None:
    """Show recent application logs (bounded)."""
    manifest = _manifest()
    try:
        code, result = commands.application_logs(_helper(manifest), tail)
    except (CommandError, ClientError) as error:
        _fail(str(error), error.code)
    sys.stdout.write(result["logs"])


@app.command()
def rollback(json_output: bool = typer.Option(False, "--json")) -> None:
    """Restore the previous successful release and its managed configuration."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "rollback",
                                              on_progress=_progress(json_output)), json_output)


@app.command()
def recover(json_output: bool = typer.Option(False, "--json")) -> None:
    """Reconcile an interrupted deployment to the last committed release."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "recover",
                                              on_progress=_progress(json_output)), json_output)


@env_app.command("set")
def env_set(from_file: Path = typer.Option(..., "--from-file", help="A KEY=VALUE file; use - for stdin."),
            json_output: bool = typer.Option(False, "--json")) -> None:
    """Replace the application's runtime environment. Values are never printed."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        try:
            text = sys.stdin.read() if str(from_file) == "-" else from_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise CommandError("could not read the environment file") from None
        return commands.set_environment(manifest, _admin(manifest), "set", text)
    _execute(action, json_output)


@env_app.command("keys")
def env_keys(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the names (never the values) of the configured environment variables."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.set_environment(manifest, _admin(manifest), "keys"), json_output)


@app.command("deprovision")
def deprovision(retire: bool = typer.Option(False, "--retire", help="Stop the running release and remove its route first."),
                yes: bool = typer.Option(False, "--yes", help="Confirm that this application's access is removed."),
                json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove this application's server-side account, keys, registration and state. Data is kept."""
    manifest = _manifest(json_output)
    if not yes:
        _fail(f"this removes {manifest['name']}'s deployment access from the server; rerun with --yes to confirm", 2, json_output)
    _execute(lambda: commands.deprovision_application(manifest, _admin(manifest), retire=retire), json_output)


@env_app.command("clear")
def env_clear(json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove the application's runtime environment file."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.set_environment(manifest, _admin(manifest), "clear"), json_output)


def _ask_tailscale(instructions: dict) -> tuple[str, str] | None:
    """Show the filled-in Tailscale steps and read the two identifiers (not secrets) from the terminal."""
    typer.echo("Tailscale is not set up for CI yet. Shiplord cannot create the trust credential for you, but here is "
               "exactly what to enter:\n")
    for number, step in enumerate(instructions["steps"], 1):
        typer.echo(f"  {number}. {step}")
    if instructions.get("warning"):
        typer.echo(f"\n  warning: {instructions['warning']}")
    typer.echo("\nPaste the values below, or press Enter to skip and add them later.")
    client_id = typer.prompt("Tailscale client ID", default="", show_default=False).strip()
    if not client_id:
        return None
    return client_id, typer.prompt("Tailscale audience", default="", show_default=False).strip()


def _tailscale_settings(client_id: Optional[str], audience: Optional[str], tags: Optional[str]) -> dict[str, str]:
    values = {"SHIPLORD_TS_CLIENT_ID": client_id, "SHIPLORD_TS_AUDIENCE": audience, "SHIPLORD_TS_TAGS": tags}
    settings = {name: value for name, value in values.items() if value}
    for name, value in settings.items():
        if not re.fullmatch(r"[\x21-\x7e]{1,512}", value):
            raise CommandError(f"the value for {name} must be printable text without spaces")
    if tags and not re.fullmatch(r"tag:[a-z0-9-]+(,tag:[a-z0-9-]+)*", tags):
        raise CommandError("--tailscale-tags must look like tag:ci or tag:ci,tag:deploy")
    return settings


@registry_app.command("login")
def registry_login_command(username: str = typer.Option(..., "--username"),
                           token_file: Optional[Path] = typer.Option(None, "--token-file", help="File holding the token."),
                           token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the token from stdin."),
                           registry: str = typer.Option("ghcr.io", "--registry"),
                           json_output: bool = typer.Option(False, "--json")) -> None:
    """Give this application a read-only registry login. The token is never taken from the command line."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        if token_stdin == (token_file is not None):
            raise CommandError("provide the token with exactly one of --token-file and --token-stdin")
        try:
            token = (sys.stdin.read() if token_stdin else token_file.read_text(encoding="utf-8")).strip()
        except (OSError, UnicodeDecodeError):
            raise CommandError("could not read the token") from None
        commands.validate_registry_login(registry, username, token)
        return commands.registry_login(manifest, _admin(manifest), registry=registry, username=username, token=token)
    _execute(action, json_output)


@registry_app.command("logout")
def registry_logout_command(registry: str = typer.Option("ghcr.io", "--registry"),
                            json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove this application's registry login from the server."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.registry_other(manifest, _admin(manifest), "logout", registry), json_output)


@registry_app.command("status")
def registry_status_command(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show which registries have a login (usernames only; tokens are never shown)."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.registry_other(manifest, _admin(manifest), "list"), json_output)


@github_app.command("setup")
def github_setup_command(repo: str = typer.Option(..., "--repo", help="owner/repository"),
                         ci_host: Optional[str] = typer.Option(None, "--ci-host", help="Server name CI should use (default: the alias host)."),
                         tooling_ref: Optional[str] = typer.Option(None, "--tooling-ref", help="Commit of Shiplord to pin in CI."),
                         platforms: str = typer.Option("linux/amd64", "--platforms"),
                         tailscale_client_id: Optional[str] = typer.Option(None, "--tailscale-client-id"),
                         tailscale_audience: Optional[str] = typer.Option(None, "--tailscale-audience"),
                         tailscale_tags: Optional[str] = typer.Option(None, "--tailscale-tags"),
                         force_workflow: bool = typer.Option(False, "--force-workflow"),
                         json_output: bool = typer.Option(False, "--json")) -> None:
    """Create the CI key, store it in GitHub, set the repository variables, and write the release workflow."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        github.validate_setup_profile(manifest)
        given = _tailscale_settings(tailscale_client_id, tailscale_audience, tailscale_tags)
        gh = github.GitHub()
        interactive = sys.stdin.isatty() and not json_output
        try:
            settings, origin = tailscale.resolve(
                given, ask=_ask_tailscale if interactive else None,
                guide=lambda: github.tailscale_guidance(gh, repo, manifest))
        except tailscale.TailscaleError as error:
            raise CommandError(str(error)) from None
        target = load_server(manifest["server"])
        result = github.github_setup(manifest, repo, project=Path.cwd(), admin=AdminClient(target), gh=gh,
                                     admin_target=target, ci_host=ci_host, tooling_ref=tooling_ref, platforms=platforms,
                                     tailscale=settings, force_workflow=force_workflow)
        result["tailscale"] = origin
        return 0, result
    _execute(action, json_output)


@github_app.command("check-ci")
def github_check_ci_command(timeout: int = typer.Option(1200, "--timeout", help="Seconds to wait for the run."),
                            json_output: bool = typer.Option(False, "--json")) -> None:
    """Run the release workflow in check-only mode: join Tailscale, log in with the CI key, deploy nothing."""
    manifest = _manifest(json_output)
    _execute(lambda: github.check_ci(manifest, gh=github.GitHub(), timeout=timeout), json_output)


@github_app.command("status")
def github_status_command(check_server: bool = typer.Option(False, "--check-server", help="Also confirm the key on the server."),
                          json_output: bool = typer.Option(False, "--json")) -> None:
    """Check the repository access, secret, variables, workflow, CI key, and the last workflow run."""
    manifest = _manifest(json_output)
    _execute(lambda: github.github_status(manifest, project=Path.cwd(), gh=github.GitHub(),
                                          admin=_admin(manifest) if check_server else None), json_output)


@github_app.command("rotate-key")
def github_rotate_key(json_output: bool = typer.Option(False, "--json")) -> None:
    """Replace the CI key: add and prove the new one, update GitHub, then revoke the old one."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        target = load_server(manifest["server"])
        return 0, github.rotate_key(manifest, admin=AdminClient(target), gh=github.GitHub(), admin_target=target)
    _execute(action, json_output)


def main() -> None:
    app()
