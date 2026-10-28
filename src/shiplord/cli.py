"""Command-line entry point for the first local milestone."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .configure import ConfigurationError, create_manifest
from .manifest import ManifestError, load_manifest
from .servers import InvalidTarget, ServerError, SSHCheckError, register_server
from .template import TemplateError, create_project

app = typer.Typer(no_args_is_help=True, help="Manage application releases. Remote operations are not implemented yet.")
server_app = typer.Typer(no_args_is_help=True, help="Manage servers (not implemented yet).")
github_app = typer.Typer(no_args_is_help=True, help="Manage GitHub deployment configuration (not implemented yet).")
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
def plan(json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Preview local configuration. Server changes are unknown."""
    try:
        manifest = load_manifest()
    except ManifestError as exc:
        _emit({"ok": False, "error": str(exc)}, json_output)
        raise typer.Exit(2) from None
    _emit({"ok": True, "scope": "local configuration preview", "remote_changes": "unknown; server state was not checked", "configuration": manifest}, json_output)


@app.command()
def doctor(local: bool = typer.Option(False, "--local", help="Check local prerequisites."),
           server: Optional[str] = typer.Option(None, "--server", help="Server alias."),
           json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Check local prerequisites; server checks are not implemented."""
    if local and server:
        _emit({"ok": False, "error": "choose either --local or --server"}, json_output)
        raise typer.Exit(2)
    if not local:
        _unavailable("server doctor", json_output)
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
    typer.echo("Server alias saved. Bootstrap and CI host-key setup are not implemented.")


@server_app.command("bootstrap")
def server_bootstrap(alias: str) -> None:
    _unavailable("server bootstrap")


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
              runtime: str = typer.Option("docker", "--runtime")) -> None:
    """Create ship.toml in the current directory without overwriting it."""
    if bool(frontend) == no_frontend:
        typer.echo("error: specify --frontend or explicitly choose --no-frontend", err=True)
        raise typer.Exit(2)
    try:
        create_manifest(Path.cwd(), name or Path.cwd().name, server, api_domain, frontend or [], runtime)
    except (ConfigurationError, ManifestError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo("Created ship.toml. The server alias has not been checked.")


@app.command()
def provision() -> None:
    _unavailable("provision")


@app.command()
def deploy(image: Optional[str] = typer.Option(None, "--image"), binary: Optional[Path] = typer.Option(None, "--binary"),
           commit: Optional[str] = typer.Option(None, "--commit"), json_output: bool = typer.Option(False, "--json")) -> None:
    _unavailable("deploy", json_output)


@app.command()
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    _unavailable("status", json_output)


@app.command()
def logs(tail: int = typer.Option(100, "--tail")) -> None:
    _unavailable("logs")


@app.command()
def rollback(json_output: bool = typer.Option(False, "--json")) -> None:
    _unavailable("rollback", json_output)


@app.command()
def recover(json_output: bool = typer.Option(False, "--json")) -> None:
    _unavailable("recover", json_output)


@github_app.command("setup")
def github_setup(repo: str = typer.Option(..., "--repo")) -> None:
    _unavailable("github setup")


@github_app.command("status")
def github_status(json_output: bool = typer.Option(False, "--json")) -> None:
    _unavailable("github status", json_output)


@github_app.command("rotate-key")
def github_rotate_key() -> None:
    _unavailable("github rotate-key")


def main() -> None:
    app()
