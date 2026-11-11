"""Command-line entry point for the first local milestone."""

from __future__ import annotations

import json
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, NoReturn, Optional

import typer

from . import __version__
from . import build, commands, github, output, setup as setup_module, tailscale, workflow
from .client import AdminClient, ClientError, HelperClient, bootstrap_server
from .commands import CommandError
from .configure import ConfigurationCancelled, ConfigurationError, configure_application, validate_repository
from .environment import EnvironmentError_, parse_environment
from .keys import KeyError_, parse_public_key, validate_label
from .manifest import ManifestError, load_manifest
from .layout import deployment_user
from .protocol import IncompatibleProtocol
from .runtime import DEFAULT_FOLLOW_SECONDS, MAX_FOLLOW_SECONDS, MAX_LOG_LINES
from .servers import (InvalidAlias, InvalidTarget, NoServerDirectory, ServerError, SshOptions, SSHCheckError,
                      UnknownServer, list_servers, load_server, register_server, remove_server, rename_server,
                      validate_target)
from .template import TemplateError, create_project
from .transport import TransportError, probe_capabilities

app = typer.Typer(no_args_is_help=True, help="Create, provision, and ship applications to a server.")
server_app = typer.Typer(no_args_is_help=True, help="Manage servers.")
github_app = typer.Typer(no_args_is_help=True, help="Configure GitHub Actions deployments.")
keys_app = typer.Typer(no_args_is_help=True, help="Manage the SSH keys that may deploy this application.")
env_app = typer.Typer(no_args_is_help=True, help="Manage an application's runtime environment on the server.")
app.add_typer(keys_app, name="keys", rich_help_panel="Administration")
app.add_typer(env_app, name="env", rich_help_panel="Administration")
registry_app = typer.Typer(no_args_is_help=True, help="Manage the server's read-only pull login for private images.")
app.add_typer(registry_app, name="registry", rich_help_panel="Administration")
app.add_typer(server_app, name="server", rich_help_panel="Setup")
app.add_typer(github_app, name="github", rich_help_panel="Administration")


def _emit(result: dict) -> None:
    """The machine contract: sorted JSON, one document, stable key order."""
    typer.echo(json.dumps(result, sort_keys=True))


def _report(result: dict, json_output: bool) -> None:
    """Print a result that has no command-specific renderer: JSON, an error, or readable key lines."""
    if json_output:
        _emit(result)
    elif result.get("ok") is False or result.get("error"):
        output.render_failure(result)
    else:
        output.render_result(result)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"shiplord {__version__}")
        raise typer.Exit()


def _client_details() -> dict[str, str]:
    invoked = Path(sys.argv[0])
    executable = str(invoked.resolve()) if invoked.exists() else (shutil.which(sys.argv[0]) or shutil.which("shiplord") or sys.argv[0])
    return {"version": __version__, "executable": executable}


@app.callback()
def root(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version.")) -> None:
    """Shiplord CLI."""


@app.command(rich_help_panel="Quick start")
def apps(server: Optional[str] = typer.Argument(None, metavar="SERVER"),
         all_servers: bool = typer.Option(False, "--all-servers", help="List applications across every registered server."),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """List registered applications and their release state."""
    try:
        servers, invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)

    if all_servers and server is not None:
        _fail("choose either a server alias or --all-servers", 2, json_output)
    aliases = [entry["alias"] for entry in servers]
    if all_servers:
        if not servers:
            result = {"ok": False, "partial": bool(invalid), "servers": [], "error": "no readable servers are registered", "invalid_entries": invalid}
            _report(result, json_output)
            raise typer.Exit(3)
        results = []
        for entry in servers:
            try:
                code, inventory = _server_inventory(entry, json_output)
                results.append({"server": entry["alias"], **inventory})
                if code:
                    results[-1]["ok"] = False
            except (CommandError, ClientError, ManifestError, ServerError) as error:
                item = {"server": entry["alias"], "ok": False, "applications": [],
                        "error": _inventory_error(error, entry["alias"])}
                if isinstance(error, ClientError) and error.error_code:
                    item["error_code"] = error.error_code
                results.append(item)
        partial = bool(invalid) or any(not result["ok"] for result in results)
        payload = {"ok": not partial, "partial": partial, "servers": results}
        if invalid:
            payload["invalid_entries"] = invalid
        if json_output:
            _emit(payload)
        else:
            for result in results:
                typer.echo(f"Server: {result['server']}  Outcome: {'partial' if not result['ok'] else 'complete'}")
                if result.get("error_code") == "helper_upgrade_required":
                    typer.echo(output.upgrade_message(result))
                else:
                    output.render_apps(result.get("applications", []), result.get("error"))
                    output.render_ignored_files(result.get("ignored_files", []))
            if invalid:
                typer.echo(f"Ignored {invalid} invalid server {'entry' if invalid == 1 else 'entries'}.")
        if partial:
            raise typer.Exit(3)
        return

    if server is None:
        if len(servers) != 1:
            if not servers:
                _fail("No servers are registered. Run `shiplord server add`, then `shiplord apps`.", 3, json_output)
            _fail("Choose a server with `shiplord apps <server>`; registered aliases: " + ", ".join(aliases),
                  3, json_output, aliases=aliases)
        selected = servers[0]
    else:
        selected = next((entry for entry in servers if entry["alias"] == server), None)
        if selected is None:
            _fail(f"Server {server!r} is not registered. Registered aliases: " + (", ".join(aliases) or "none"),
                  3, json_output, aliases=aliases)
    try:
        code, inventory = _server_inventory(selected, json_output)
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_inventory_error(error, selected["alias"]), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    payload = {**inventory, "server": selected["alias"]}
    if invalid:
        payload["invalid_entries"] = invalid
    if json_output:
        _emit(payload)
    elif inventory.get("error_code") == "helper_upgrade_required":
        typer.echo(output.upgrade_message(payload))
    else:
        typer.echo(f"Server: {selected['alias']}  Outcome: {'complete' if inventory['ok'] else 'incomplete'}")
        output.render_apps(inventory.get("applications", []), inventory.get("error"))
        output.render_ignored_files(inventory.get("ignored_files", []))
        if invalid:
            typer.echo(f"Ignored {invalid} invalid server {'entry' if invalid == 1 else 'entries'}.")
    if code:
        raise typer.Exit(code)


def _server_inventory(server: dict, json_output: bool) -> tuple[int, dict]:
    """Reject confirmed old helpers before attempting the privileged request."""
    try:
        response = probe_capabilities(server["ssh"], timeout=5)
        version = response.get("helper_version")
    except (TransportError, IncompatibleProtocol, ServerError):
        version = None
    upgrade = commands.inventory_upgrade(server["alias"], version)
    if upgrade is not None:
        return 4, {"ok": False, **upgrade}
    prompt = None if json_output else _interactive_admin_prompt(False)
    return commands.server_applications(AdminClient(server["ssh"], prompt=prompt))


def _inventory_error(error: Exception, alias: str) -> str:
    message = _recovery_hint(str(error))
    if isinstance(error, ClientError) and error.error_code == "invalid_request":
        message += (f"; the server helper may not support inventory. Run `shiplord doctor --server {alias} --deep` "
                    f"to inspect it, or `shiplord server bootstrap {alias}` to update the helper.")
    return message


@app.command(rich_help_panel="Quick start")
def plan(image: Optional[str] = typer.Option(None, "--image", help="Ask the server what deploying this image would do."),
         binary: Optional[Path] = typer.Option(None, "--binary", help="Ask what deploying this binary would do (not uploaded)."),
         commit: Optional[str] = typer.Option(None, "--commit"),
         sequence: Optional[int] = typer.Option(None, "--sequence", rich_help_panel="Advanced options"),
         allow_older: bool = typer.Option(False, "--allow-older", rich_help_panel="Advanced options"),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Preview changes: locally, or with --image, what the server would do (read-only)."""
    manifest = _manifest(json_output)
    if image is None and binary is None:
        result = {"ok": True, "scope": "local configuration preview",
                  "remote_changes": "unknown; pass --image or --binary to ask the server", "configuration": manifest}
        if json_output:
            _emit(result)
        else:
            typer.echo("Target: local configuration")
            typer.echo("Outcome: preview ready")
            typer.echo("Remote changes: unknown; pass --image or --binary to ask the server")
        return

    def action() -> tuple[int, dict]:
        sha = commit or _head_commit()
        commands.validate_deploy_inputs(manifest, binary if binary is not None else image, sha)
        return commands.plan_application(manifest, _helper(manifest), image=image, binary=binary, commit=sha,
                                         sequence=commands.commit_sequence(sha, sequence), allow_older=allow_older)
    _execute(action, json_output, lambda code, result: output.render_operation(
        code, result, f"{manifest['name']} on {manifest['server']}", "plan"))


@app.command(rich_help_panel="Troubleshooting")
def doctor(local: bool = typer.Option(False, "--local", help="Check local prerequisites."),
           server: Optional[str] = typer.Option(None, "--server", help="Server alias."),
           all_servers: bool = typer.Option(False, "--all-servers",
                                            help="Probe every registered server (read-only, no sudo)."),
           deep: bool = typer.Option(False, "--deep", help="With --server: also check Caddy, sshd, ports, Docker, disk, and applications (sudo)."),
           json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Check local prerequisites or a server's protocol capabilities; without a manifest, check local only."""
    if all_servers:
        for name, chosen in (("--local", local), ("--server", server is not None), ("--deep", deep)):
            if chosen:
                _report({"ok": False, "error": f"--all-servers cannot be combined with {name}"}, json_output)
                raise typer.Exit(2)
        _doctor_all_servers(json_output)
        return
    if local and server:
        _report({"ok": False, "error": "choose either --local or --server"}, json_output)
        raise typer.Exit(2)
    if server and deep:
        def action() -> tuple[int, dict]:
            code, result = commands.server_doctor(_admin_target(load_server(server), json_output))
            result["client"] = _client_details()
            return code, result
        _execute(action, json_output, lambda code, result: output.render_doctor(result, f"server {server}"))
        return
    if server:
        try:
            response = probe_capabilities(load_server(server))
        except ManifestError as exc:
            _report({"ok": False, "error": str(exc)}, json_output)
            raise typer.Exit(2) from None
        except IncompatibleProtocol:
            _report({"ok": False, "error": "incompatible helper protocol version"}, json_output)
            raise typer.Exit(4) from None
        except ServerError:
            _report({"ok": False, "error": "server capabilities check failed; verify the alias, SSH trust, login, and dispatcher"}, json_output)
            raise typer.Exit(3) from None
        except TransportError as exc:
            _report({"ok": False, "error": str(exc)}, json_output)
            raise typer.Exit(3) from None
        result = {"ok": True, "scope": "protocol capabilities only; server readiness and deployment access were not checked",
                  "helper_version": response["helper_version"], "client": _client_details()}
        if json_output:
            _emit(result)
        else:
            typer.echo(f"Target: server {server}")
            typer.echo("Outcome: protocol reachable")
            typer.echo(f"Client: shiplord {__version__} at {result['client']['executable']}")
            typer.echo(f"Server helper: {response['helper_version']}")
        return
    if local or not _manifest_present():
        result = _local_checks()
        if json_output:
            _emit(result)
        else:
            output.render_doctor(result, "local machine")
            if not local:
                typer.echo("Server readiness was not checked: this directory has no ship.toml.")
                typer.echo("Next: run `shiplord configure` here to create an application, or run "
                           "`shiplord doctor --server <alias>` to check a registered server.")
        if not result["ok"]:
            raise typer.Exit(3)
        return
    manifest = _manifest(json_output)
    def action() -> tuple[int, dict]:
        code, result = commands.application_doctor(manifest, _helper(manifest))
        result["client"] = _client_details()
        return code, result
    _execute(action, json_output, lambda code, result: output.render_doctor(
        result, f"{manifest['name']} on {manifest['server']}"))


def _doctor_all_servers(json_output: bool) -> None:
    """Probe every registered server once, continuing past a failure; sudo and bootstrap are never involved."""
    try:
        servers, invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)
    if not servers:
        _fail("No servers are registered. Run `shiplord server add`, then `shiplord doctor --all-servers`.",
              3, json_output)
    code, result = commands.doctor_all_servers(servers)
    if invalid:
        result.update(ok=False, partial=True, invalid_entries=invalid)
        code = 3
    result["client"] = _client_details()
    if json_output:
        _emit(result)
    else:
        output.render_fleet_doctor(result)
    if code:
        raise typer.Exit(code)


@server_app.command("add", rich_help_panel="Setup")
def server_add(alias: str, ssh: str = typer.Option(..., "--ssh"),
               json_output: bool = typer.Option(False, "--json")) -> None:
    """Register a server using an already trusted OpenSSH login."""
    try:
        register_server(alias, ssh)
    except (ManifestError, ServerError) as exc:
        code = 2 if isinstance(exc, (ManifestError, InvalidTarget)) else 3 if isinstance(exc, SSHCheckError) else 1
        _setup_fail(str(exc), code, json_output)
    result = {"ok": True, "alias": alias, "ssh": ssh}
    if json_output:
        _emit(result)
    else:
        output.render_server_add(result)


@server_app.command("remove", rich_help_panel="Administration")
def server_remove(alias: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove a registered server alias (local only; nothing is contacted)."""
    try:
        remove_server(alias)
    except (ManifestError, ServerError) as error:
        _server_failure(error, json_output)
    if json_output:
        _emit({"ok": True, "removed": alias})
        return
    typer.echo(f"Removed server alias {alias!r}.")


@server_app.command("rename", rich_help_panel="Administration")
def server_rename(old: str, new: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Rename a registered server alias (local only; nothing is contacted)."""
    try:
        rename_server(old, new)
    except (ManifestError, ServerError) as error:
        _server_failure(error, json_output)
    if json_output:
        _emit({"ok": True, "alias": new, "previous_alias": old})
        return
    typer.echo(f"Renamed server alias {old!r} to {new!r}.")


@server_app.command("list", rich_help_panel="Administration")
def server_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the registered servers (local only; nothing is contacted)."""
    try:
        servers, invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)
    if json_output:
        _emit({"ok": True, "servers": servers, "invalid_entries": invalid})
        return
    output.render_server_list(servers, invalid)


def _fail(message: str, code: int, json_output: bool = False, **details) -> None:
    result = {"ok": False, "error": message, **details}
    if json_output:
        _emit(result)
    else:
        output.render_failure(result)
    raise typer.Exit(code)


def _setup_fail(message: str, code: int, json_output: bool) -> NoReturn:
    """Fail a setup command (init, configure, server add) in the form the caller asked for."""
    if json_output:
        _emit({"ok": False, "error": message})
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _server_failure(error: Exception, json_output: bool = False) -> NoReturn:
    """Exit with the code that matches a local server-registration failure."""
    if isinstance(error, (ManifestError, InvalidAlias, InvalidTarget)):
        code = 2
    elif isinstance(error, (NoServerDirectory, UnknownServer, SSHCheckError)):
        code = 3
    else:
        code = 1
    _fail(str(error), code, json_output)


def _manifest(json_output: bool = False) -> dict:
    try:
        return load_manifest()
    except ManifestError as error:
        _fail(str(error), 2, json_output)


def _manifest_present() -> bool:
    """A ship.toml that exists stays authoritative, even when it is a link that does not resolve."""
    return Path("ship.toml").exists() or Path("ship.toml").is_symlink()


def _local_checks() -> dict:
    """The local prerequisite report shared by `--local` and the fallback when no manifest is present."""
    checks = {name: bool(shutil.which(name)) for name in ("ssh", "git", "gh")}
    return {"ok": all(checks.values()), "scope": "local prerequisites only; server readiness was not checked",
            "checks": checks, "client": _client_details()}


def _helper(manifest: dict) -> HelperClient:
    """The application's client. CI sets SHIPLORD_SSH_TARGET (the deployment account) and pinned SSH files."""
    options = SshOptions.from_environment(os.environ)
    target = os.environ.get("SHIPLORD_SSH_TARGET")
    if target:
        return HelperClient(manifest["name"], validate_target(target), ssh_options=options, explicit_target=True)
    return HelperClient(manifest["name"], load_server(manifest["server"]), ssh_options=options)


def _interactive_admin_prompt(json_output: bool) -> Callable[[str], str] | None:
    return getpass.getpass if sys.stdin.isatty() and sys.stderr.isatty() else None


def _admin_target(target: str, json_output: bool) -> AdminClient:
    return AdminClient(target, prompt=_interactive_admin_prompt(json_output))


def _admin(manifest: dict, json_output: bool = False) -> AdminClient:
    return _admin_target(load_server(manifest["server"]), json_output)


def _recovery_hint(message: str) -> str:
    """Add recovery guidance only when the reported failure identifies unfinished state."""
    if (re.search(r"recovery_required|unfinished transaction|recovery is required|reconciliation", message, re.I)
            and "shiplord recover" not in message):
        return message + "; an unfinished deployment transaction is blocking this operation. " \
               "Run `shiplord recover` from the application's directory to reconcile it."
    return message


def _select_server(server: str | None, preferred: str | None, json_output: bool) -> tuple[str, str]:
    try:
        servers, _invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)
    aliases = [entry["alias"] for entry in servers]
    selected_alias = server or preferred
    if selected_alias is not None:
        selected = next((entry for entry in servers if entry["alias"] == selected_alias), None)
        if selected is None:
            _fail(f"Server {selected_alias!r} is not registered. Registered aliases: " + (", ".join(aliases) or "none"),
                  3, json_output, aliases=aliases)
        return selected["alias"], selected["ssh"]
    if len(servers) == 1:
        return servers[0]["alias"], servers[0]["ssh"]
    if not servers:
        _fail("No servers are registered. Run `shiplord server add` or use a configured app repository.",
              3, json_output, aliases=[])
    _fail("Choose a server with `--server`; registered aliases: " + ", ".join(aliases), 3, json_output,
          aliases=aliases)


def _app_helper(application: str, server_target: str | None, *, json_output: bool,
                allow_environment_target: bool = True) -> HelperClient:
    options = SshOptions.from_environment(os.environ)
    pinned = os.environ.get("SHIPLORD_SSH_TARGET") if allow_environment_target else None
    if pinned:
        target = validate_target(pinned)
        if target.split("@", 1)[0] != deployment_user(application):
            raise CommandError("SHIPLORD_SSH_TARGET does not match the selected application's deployment account", 3)
        if server_target is not None and target.split("@", 1)[1] != server_target.split("@", 1)[1]:
            raise CommandError("SHIPLORD_SSH_TARGET does not match the selected server", 3)
        return HelperClient(application, target, ssh_options=options, explicit_target=True)
    if server_target is None:
        raise CommandError("a registered server is required; run `shiplord apps` to discover servers", 3)
    return HelperClient(application, server_target, ssh_options=options)


def _app_context(application: str | None, server: str | None, json_output: bool, command: str):
    """Resolve an app repo's manifest or a named app/server pair without changing app-scoped SSH access."""
    manifest = None
    if application is None:
        try:
            manifest = load_manifest()
        except ManifestError as error:
            if command == "logs" and "not found" in str(error):
                _fail("ship.toml was not found. Run `shiplord logs <app> --server <alias>` from anywhere, or run "
                      "`shiplord apps` to discover registered applications and servers.", 2, json_output)
            _fail(str(error), 2, json_output)
        application = manifest["name"]
    else:
        try:
            application = validate_target_application(application)
        except ManifestError as error:
            _fail(str(error), 2, json_output)
        try:
            candidate = load_manifest()
            if candidate["name"] == application:
                manifest = candidate
        except ManifestError:
            pass

    preferred = manifest["server"] if manifest and manifest["name"] == application else None
    if server is None and application == (manifest or {}).get("name") and os.environ.get("SHIPLORD_SSH_TARGET"):
        return application, preferred, _app_helper(application, None, json_output=json_output)
    selected_alias, target = _select_server(server, preferred, json_output)
    if manifest is not None and manifest["name"] == application and server is None:
        client = _helper(manifest)
    else:
        client = _app_helper(application, target, json_output=json_output)
    return application, selected_alias, client


def validate_target_application(application: str) -> str:
    from .manifest import validate_app_name
    return validate_app_name(application)


def _progress(json_output: bool) -> Callable[[str], None]:
    return (lambda phase: None) if json_output else (lambda phase: typer.echo(f"  ... {phase}", err=True))


def _execute(action: Callable[[], tuple[int, dict]], json_output: bool,
             human_renderer: Callable[[int, dict], None] | None = None) -> None:
    """Run a command function, print its result, and exit with its code; map known errors to exit codes."""
    try:
        code, result = action()
    except (CommandError, ClientError, github.GitHubError) as error:
        _fail(_recovery_hint(str(error)), error.code, json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    except (ManifestError, ServerError) as error:
        _fail(str(error), 2 if isinstance(error, (ManifestError, InvalidTarget)) else 3, json_output)
    if json_output or human_renderer is None:
        _report(result, json_output)
    else:
        human_renderer(code, result)
    if code:
        raise typer.Exit(code)


@server_app.command("bootstrap", rich_help_panel="Setup")
def server_bootstrap(alias: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Install or update the helper on a registered server (asks for the sudo password if needed)."""
    try:
        result = bootstrap_server(load_server(alias), Path(__file__).resolve().parent,
                                  prompt=_interactive_admin_prompt(json_output))
    except (ClientError, ServerError, ManifestError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    payload = {"ok": True, **result}
    if json_output:
        _emit(payload)
    else:
        output.render_bootstrap(payload, f"server {alias}")


def _render_uninstall(result: dict) -> None:
    removed = result.get("removed", {})
    typer.echo("Removed:")
    caddy = removed.get("caddy") or {}
    lines = []
    if caddy.get("import"):
        lines.append("the managed Caddy import")
    if caddy.get("placeholder"):
        lines.append("the managed Caddy placeholder route")
    if removed.get("sshd"):
        lines.append("the sshd drop-in")
    if removed.get("boot_recovery"):
        lines.append("the boot recovery unit")
    if removed.get("wrappers"):
        lines.append("the launchers: " + ", ".join(removed["wrappers"]))
    if removed.get("versions"):
        lines.append(f"the helper code ({removed['versions']} version"
                     f"{'' if removed['versions'] == 1 else 's'})")
    if removed.get("directories"):
        lines.append("empty managed directories: " + ", ".join(removed["directories"]))
    for line in lines or ["nothing (the host was already uninstalled)"]:
        typer.echo(f"  - {line}")
    retained = result.get("retained") or []
    if retained:
        typer.echo("Left in place:")
        for item in retained:
            typer.echo(f"  - {item}")
    if result.get("note"):
        typer.echo(str(result["note"]))
    typer.echo("Run `shiplord server bootstrap` to install the helper again.")


@server_app.command("uninstall", rich_help_panel="Administration")
def server_uninstall(alias: str,
                     force: bool = typer.Option(
                         False, "--force",
                         help="Remove the host even though applications are still provisioned; they keep their data."),
                     json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove the helper, launchers, sshd drop-in, and Caddy import from a server. Application data is kept."""
    def action() -> tuple[int, dict]:
        result = _admin_target(load_server(alias), json_output).call("uninstall", None, {"force": force}, timeout=300)
        return 0, {"ok": True, **result,
                   "note": "Application data is never deleted; remove it by hand when you are sure."}
    _execute(action, json_output, lambda code, result: _render_uninstall(result))


@app.command("init", rich_help_panel="Setup")
def init(name: str, template: str = typer.Option(..., "--template"),
         json_output: bool = typer.Option(False, "--json")) -> None:
    """Create an application from the pinned template snapshot."""
    try:
        create_project(Path.cwd(), name, template)
    except (ManifestError, TemplateError) as exc:
        _setup_fail(str(exc), 1 if isinstance(exc, TemplateError) else 2, json_output)
    result = {"ok": True, "name": name, "template": template}
    if json_output:
        _emit(result)
    else:
        output.render_init(result)


def _interactive() -> bool:
    """Prompt only when a person is at a terminal; agents, CI, and pipes keep the flag-only behaviour."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str, default: str | None) -> str:
    if default is None:
        return typer.prompt(prompt)
    return typer.prompt(prompt, default=default, show_default=bool(default))


def _confirm_manifest(answers: dict) -> bool:
    """Show what will be written and ask; the summary is the last chance to stop before the file exists."""
    typer.echo("\nship.toml will contain:")
    summary = [("name", "name"), ("server", "server"), ("template", "template"), ("runtime", "runtime")]
    if answers["runtime"] == "docker":
        summary.append(("image repository", "image_repository"))
    summary += [("hostname", "api_domain"), ("frontend origins", "origins"),
                ("container port", "container_port"), ("health path", "readiness_path"),
                ("memory (MiB)", "memory_mb")]
    for label, key in summary:
        value = answers[key]
        typer.echo(f"  {label}: {', '.join(value) or '(none)' if isinstance(value, list) else value}")
    if answers.get("hostnames"):
        typer.echo(f"  additional hostnames: {', '.join(answers['hostnames'])}")
    if answers.get("www_redirect"):
        typer.echo(f"  www redirect: www.{answers['api_domain']}")
    return typer.confirm("Write ship.toml?", default=True)


@app.command(rich_help_panel="Setup")
def configure(server: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
              api_domain: Optional[str] = typer.Option(None, "--api-domain", help="Public hostname of the application."),
              hostname: Optional[list[str]] = typer.Option(
                  None, "--hostname", help="Additional hostname that redirects to --api-domain (repeatable)."),
              www: bool = typer.Option(False, "--www",
                                       help="Also redirect www.<api-domain> to the canonical hostname."),
              frontend: Optional[list[str]] = typer.Option(None, "--frontend"),
              no_frontend: bool = typer.Option(False, "--no-frontend"),
              name: Optional[str] = typer.Option(None, "--name"),
              runtime: Optional[str] = typer.Option(None, "--runtime", help="docker (default) or native."),
              template: Optional[str] = typer.Option(None, "--template", help="google-adk (default) or generic."),
              container_port: Optional[int] = typer.Option(None, "--container-port",
                                                           help="Port the application listens on in its container."),
              health_path: Optional[str] = typer.Option(None, "--health-path",
                                                        help="Path that returns 200 when the application is ready."),
              memory: Optional[int] = typer.Option(None, "--memory", help="Memory limit in MiB."),
              image_repository: Optional[str] = typer.Option(
                  None, "--image-repository",
                  help="owner/repository that publishes this app's image (default: from the git remote)."),
              json_output: bool = typer.Option(False, "--json")) -> None:
    """Create ship.toml in the current directory without overwriting it. At a terminal, missing values are asked."""
    if frontend and no_frontend:
        _setup_fail("specify --frontend or explicitly choose --no-frontend", 2, json_output)
    if image_repository is not None:
        if runtime == "native":
            _setup_fail("--image-repository applies only to Docker applications", 2, json_output)
        try:
            image_repository = validate_repository(image_repository)
        except ManifestError as error:
            _setup_fail(str(error), 2, json_output)
    given = {"server": server, "api_domain": api_domain, "name": name, "runtime": runtime, "template": template,
             "origins": list(frontend) if frontend else ([] if no_frontend else None),
             "container_port": container_port, "readiness_path": health_path, "memory_mb": memory,
             "image_repository": image_repository}
    directory = Path.cwd()
    interactive = _interactive() and not json_output  # --json never prompts: its output must be deterministic

    def servers() -> list[dict[str, str]]:
        return list_servers()[0] if server is None else []

    try:
        answers = configure_application(directory, given, interactive=interactive,
                                        servers=servers() if interactive else None,
                                        ask=_ask if interactive else None, say=typer.echo,
                                        confirm=_confirm_manifest if interactive else None,
                                        hostnames=hostname or (), www_redirect=www)
        written = load_manifest(directory / "ship.toml")
    except (ConfigurationCancelled, typer.Abort):
        _setup_fail("cancelled; no file was created", 1, json_output)
    except ServerError as error:
        _setup_fail(str(error), 3, json_output)
    except (ConfigurationError, ManifestError) as error:
        _setup_fail(str(error), 2, json_output)
    if not json_output and answers["runtime"] == "docker" and answers["image_repository"] is None:
        typer.echo("warning: no image repository was recorded, so `shiplord provision` will need "
                   "--image-repository. Pass --image-repository owner/repository to record it now.", err=True)
    try:
        registered = any(entry["alias"] == answers["server"] for entry in list_servers()[0])
    except ServerError:
        registered = False
    result = {"ok": True, "name": written["name"], "server": written["server"],
              "api_domain": written["http"]["api_domain"], "frontend_origins": written["http"]["frontend_origins"],
              "runtime": written["runtime"], "template": written["template"],
              "container_port": written["http"]["container_port"], "readiness_path": written["http"]["readiness_path"],
              "memory_mb": written["resources"]["memory_mb"], "registered": registered}
    if json_output:
        _emit(result)
    else:
        output.render_configure(result)


@app.command(rich_help_panel="Setup")
def provision(public_key: Optional[Path] = typer.Option(None, "--public-key", help="Public key to register."),
              image_repository: Optional[str] = typer.Option(
                  None, "--image-repository",
                  help="Image repository to allow (default: from ship.toml)."),
              max_memory_mb: Optional[int] = typer.Option(None, "--max-memory-mb", help="Ceiling for memory_mb (default 4096)."),
              update: bool = typer.Option(False, "--update", help="Apply a changed hostname or memory ceiling to an "
                                                                  "existing application; keep its state and keys."),
              json_output: bool = typer.Option(False, "--json")) -> None:
    """Create the application's account, route-ready port, state, and key access on the server.

    With --update, apply a changed hostname or memory ceiling without removing the application's state, environment,
    or keys. Changing the runtime or image repository is refused before anything is touched.
    """
    manifest = _manifest(json_output)
    target = f"{manifest['name']} on {manifest['server']}"
    _execute(lambda: commands.provision_application(manifest, _admin(manifest, json_output), public_key=public_key,
                                                    image_repository=image_repository,
                                                    max_memory_mb=max_memory_mb, update=update), json_output,
             lambda code, result: output.render_provision(result, target))


def _head_commit(hint: str = "pass --commit") -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CommandError(f"could not run git; {hint}", 3) from None
    if result.returncode != 0:
        raise CommandError(f"could not read HEAD; {hint}", 2)
    return result.stdout.strip()


@app.command(rich_help_panel="Quick start")
def deploy(image: Optional[str] = typer.Option(None, "--image", help="Immutable image: registry/name@sha256:<digest>."),
           binary: Optional[Path] = typer.Option(None, "--binary"), commit: Optional[str] = typer.Option(None, "--commit"),
           sequence: Optional[int] = typer.Option(None, "--sequence", help="Release ordering key; default: the commit time.",
                                                  rich_help_panel="Advanced options"),
           request_id: Optional[str] = typer.Option(None, "--request-id", help="Reuse to retry a deployment safely.",
                                                    rich_help_panel="Advanced options"),
           allow_older: bool = typer.Option(False, "--allow-older", rich_help_panel="Advanced options"),
           timeout: Optional[int] = typer.Option(None, "--timeout", rich_help_panel="Advanced options"),
           require_public: bool = typer.Option(False, "--require-public", help="Fail if the public HTTPS check fails.",
                                               rich_help_panel="Advanced options"),
           verify_public: bool = typer.Option(True, "--verify-public/--no-verify-public",
                                              rich_help_panel="Advanced options"),
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
    _execute(action, json_output, lambda code, result: output.render_operation(
        code, result, f"{manifest['name']} on {manifest['server']}", "deployment"))


@app.command(rich_help_panel="Quick start")
def release(image_repository: Optional[str] = typer.Option(None, "--image-repository",
                                                          help="Image repository to push to; default from [github]."),
            tag: Optional[str] = typer.Option(None, "--tag", help="Image tag to push (default: the commit SHA)."),
            platform: str = typer.Option(build.DEFAULT_PLATFORM, "--platform",
                                         help="Server platform: linux/amd64 or linux/arm64."),
            sequence: Optional[int] = typer.Option(None, "--sequence",
                                                   help="Release ordering key; default: the commit time.",
                                                   rich_help_panel="Advanced options"),
            request_id: Optional[str] = typer.Option(None, "--request-id",
                                                     help="Reuse to retry a deployment safely.",
                                                     rich_help_panel="Advanced options"),
            allow_older: bool = typer.Option(False, "--allow-older", rich_help_panel="Advanced options"),
            timeout: Optional[int] = typer.Option(None, "--timeout", rich_help_panel="Advanced options"),
            require_public: bool = typer.Option(False, "--require-public",
                                                help="Fail if the public HTTPS check fails.",
                                                rich_help_panel="Advanced options"),
            verify_public: bool = typer.Option(True, "--verify-public/--no-verify-public",
                                               rich_help_panel="Advanced options"),
            json_output: bool = typer.Option(False, "--json")) -> None:
    """Build and push this checkout's image, then deploy the pushed digest."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        commit = _head_commit("run `shiplord release` inside the application's Git repository")
        return commands.release_application(
            manifest, _helper(manifest), commit=commit, image_repository=image_repository, tag=tag, platform=platform,
            sequence=commands.commit_sequence(commit, sequence), request_id=request_id, timeout=timeout,
            allow_older=allow_older, on_progress=_progress(json_output), verify_public=verify_public,
            require_public=require_public)
    _execute(action, json_output, lambda code, result: output.render_operation(
        code, result, f"{manifest['name']} on {manifest['server']}", "release"))


@app.command(rich_help_panel="Quick start")
def status(application: Optional[str] = typer.Argument(None, metavar="APP"),
           server: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
           job_id: Optional[str] = typer.Option(None, "--job-id", help="Look up a deployment job by request ID."),
           json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the deployed release, the previous release, and any unfinished transaction."""
    try:
        app_name, server_alias, client = _app_context(application, server, json_output, "status")
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    try:
        _code, result = commands.application_status(client, job_id)
    except (CommandError, ClientError) as error:
        _fail(_recovery_hint(str(error)), error.code, json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    if job_id is not None and result.get("job") is None:
        target = f"{app_name} on {server_alias}" if server_alias else app_name
        _fail(f"no job {job_id!r} is recorded for {target}; it may have expired", 1, json_output)
    if json_output:
        _emit(result)
    else:
        output.render_status(app_name, server_alias, result)


@app.command(rich_help_panel="Quick start")
def logs(application: Optional[str] = typer.Argument(None, metavar="APP"),
         server: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
         tail: int = typer.Option(100, "--tail", help=f"Show at most this many lines (1-{MAX_LOG_LINES})."),
         since: Optional[str] = typer.Option(None, "--since",
                                            help="Only lines newer than a duration: 30s, 15m or 2h."),
         timestamps: bool = typer.Option(False, "--timestamps", help="Prefix each line with its timestamp."),
         follow: bool = typer.Option(False, "--follow", "-f",
                                     help="Stream new lines until interrupted or --timeout expires."),
         timeout: int = typer.Option(DEFAULT_FOLLOW_SECONDS, "--timeout",
                                     help=f"Stop following after this many seconds (1-{MAX_FOLLOW_SECONDS}, "
                                          f"default {DEFAULT_FOLLOW_SECONDS})."),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Show the current release's logs, optionally following them.

    Timestamps are off unless --timestamps is given. --follow streams the current release's
    output and is bounded: it stops after --timeout seconds (1-3600, default 300) or when
    interrupted with Ctrl-C.
    """
    if follow and json_output:
        _fail("--follow streams text and cannot be combined with --json", 2, json_output)
    try:
        app_name, server_alias, client = _app_context(application, server, json_output, "logs")
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    target = f"{app_name} on {server_alias}" if server_alias else app_name
    if not json_output:
        typer.echo(f"Target: {target}", err=True)

    def write(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    try:
        _code, result = commands.application_logs(client, tail, since=since, timestamps=timestamps,
                                                  follow=follow, timeout=timeout, on_chunk=write)
    except KeyboardInterrupt:
        typer.echo("Stopped following logs.", err=True)
        raise typer.Exit(130)
    except (CommandError, ClientError) as error:
        _fail(_recovery_hint(str(error)), error.code, json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    if json_output:
        _emit(result)
        return
    if follow:
        stopped = result.get("stopped")
        outcome = {"timeout": "stopped at the timeout", "ended": "the stream ended",
                   "unavailable": "no logs for the current release"}.get(stopped, stopped)
        release = result.get("release")
        typer.echo(f"Outcome: {outcome}" + (f" (release {release})" if release else ""), err=True)
        return
    lines = len(result["logs"].splitlines())
    typer.echo(f"Outcome: retrieved {lines} log {'line' if lines == 1 else 'lines'}", err=True)
    sys.stdout.write(result["logs"])


@app.command(rich_help_panel="Administration")
def rollback(json_output: bool = typer.Option(False, "--json")) -> None:
    """Restore the previous successful release and its managed configuration."""
    manifest = _manifest(json_output)
    target = f"{manifest['name']} on {manifest['server']}"
    _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "rollback",
                                              on_progress=_progress(json_output)), json_output,
             lambda code, result: output.render_job(result, target, "rollback"))


@app.command(rich_help_panel="Administration")
def restart(json_output: bool = typer.Option(False, "--json")) -> None:
    """Restart the deployed release so it reads the current environment."""
    manifest = _manifest(json_output)
    target = f"{manifest['name']} on {manifest['server']}"
    _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "restart",
                                              on_progress=_progress(json_output)), json_output,
             lambda code, result: output.render_job(result, target, "restart"))


@app.command(rich_help_panel="Troubleshooting")
def recover(application: Optional[str] = typer.Argument(None, metavar="APP"),
            server: Optional[str] = typer.Option(None, "--server", help="Registered server alias; required with APP."),
            force_stop: bool = typer.Option(
                False, "--force-stop",
                help="Kill a process that outlived a fail-policy shutdown deadline; the application stays stopped."),
            json_output: bool = typer.Option(False, "--json")) -> None:
    """Reconcile an interrupted deployment to the last committed release."""
    if application is None and server is None:
        manifest = _manifest(json_output)
        _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "recover", force_stop=force_stop,
                                                  on_progress=_progress(json_output)), json_output,
                 lambda code, result: output.render_job(
                     result, f"{manifest['name']} on {manifest['server']}", "recover"))
        return
    if application is None or server is None:
        _fail("recovery needs both an application and a server alias: run "
              "`shiplord recover <app> --server <alias>` from anywhere, or `shiplord recover` in the application's "
              "directory", 2, json_output)
    try:
        app_name, server_alias, client = _app_context(application, server, json_output, "recover")
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    _execute(lambda: commands.run_job_command(None, client, "recover", force_stop=force_stop,
                                              on_progress=_progress(json_output)), json_output,
             lambda _code, result: output.render_job(result, f"{app_name} on {server_alias}", "recover"))


@app.command(rich_help_panel="Administration")
def backup(json_output: bool = typer.Option(False, "--json")) -> None:
    """Archive this application's persistent data on the server and verify the archive."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.backup_application(manifest, _helper(manifest), on_progress=_progress(json_output)),
             json_output, lambda code, result: output.render_operation(
                 code, result, f"{manifest['name']} on {manifest['server']}", "backup"))


@app.command(rich_help_panel="Administration")
def restore(archive: Optional[str] = typer.Option(None, "--archive", help="An archive name reported by `shiplord backup`."),
            latest: bool = typer.Option(False, "--latest", help="Use the most recent backup."),
            yes: bool = typer.Option(False, "--yes", help="Confirm that this application's live data is replaced."),
            json_output: bool = typer.Option(False, "--json")) -> None:
    """Replace this application's persistent data with a verified backup."""
    manifest = _manifest(json_output)
    if not yes:
        _fail(f"a restore replaces {manifest['name']}'s live data on the server; rerun with --yes to confirm", 2,
              json_output)

    def action() -> tuple[int, dict]:
        arguments = commands.restore_arguments(archive, latest)  # validated before any connection is made
        return commands.restore_application(manifest, _helper(manifest), arguments,
                                            on_progress=_progress(json_output))
    _execute(action, json_output, lambda code, result: output.render_operation(
        code, result, f"{manifest['name']} on {manifest['server']}", "restore"))


@env_app.command("set")
def env_set(assignment: Optional[str] = typer.Argument(None, metavar="KEY=VALUE", help="Set one variable."),
            from_file: Optional[Path] = typer.Option(None, "--from-file",
                                                     help="Replace the whole environment from a KEY=VALUE file; use - for stdin."),
            json_output: bool = typer.Option(False, "--json")) -> None:
    """Set one variable, or replace the runtime environment. Values are never printed."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        if (assignment is None) == (from_file is None):
            raise CommandError("choose exactly one of a KEY=VALUE assignment and --from-file")
        if assignment is not None:
            commands.environment_edit_arguments("set-key", assignment=assignment)  # before any connection
            return commands.edit_environment(manifest, _admin(manifest, json_output), "set-key", assignment=assignment)
        try:
            text = sys.stdin.read() if str(from_file) == "-" else from_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise CommandError("could not read the environment file") from None
        return commands.set_environment(manifest, _admin(manifest, json_output), "set", text)
    _execute(action, json_output, lambda code, result: output.render_environment(
        result, f"{manifest['name']} on {manifest['server']}", "set"))


@env_app.command("diff")
def env_diff(from_file: Path = typer.Option(..., "--from-file", help="A KEY=VALUE file; use - for stdin."),
             json_output: bool = typer.Option(False, "--json")) -> None:
    """Preview which key names `env set` would add or remove. Values are never printed or compared."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        try:
            text = sys.stdin.read() if str(from_file) == "-" else from_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise CommandError("could not read the environment file") from None
        return commands.diff_environment(manifest, _admin(manifest, json_output), text)
    _execute(action, json_output, lambda _code, result: output.render_environment_diff(result))


@env_app.command("unset")
def env_unset(key: str = typer.Argument(..., metavar="KEY", help="Remove one variable."),
              json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove one variable; every other line is left unchanged. Values are never printed."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        commands.environment_edit_arguments("unset", key=key)  # before any connection
        return commands.edit_environment(manifest, _admin(manifest, json_output), "unset", key=key)
    _execute(action, json_output, lambda code, result: output.render_environment(
        result, f"{manifest['name']} on {manifest['server']}", "unset"))


@env_app.command("keys")
def env_keys(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the names (never the values) of the configured environment variables."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.set_environment(manifest, _admin(manifest, json_output), "keys"), json_output,
             lambda code, result: output.render_environment(
                 result, f"{manifest['name']} on {manifest['server']}", "keys"))


@app.command("deprovision", rich_help_panel="Administration")
def deprovision(app_name: Optional[str] = typer.Argument(None, metavar="APP",
                                                       help="Application name; pass with --server to work outside its checkout."),
                server_alias: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
                retire: bool = typer.Option(False, "--retire", help="Stop the running release and remove its route first."),
                yes: bool = typer.Option(False, "--yes", help="Confirm that this application's access is removed."),
                json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove an application's server-side account, keys, registration and state. Data is kept.

    Run it from the application's directory, or name the application and its server from anywhere.
    """
    if (app_name is None) != (server_alias is None):
        _fail("pass both the application name and --server <alias>, or neither to use ship.toml", 2, json_output)
    manifest = None
    if app_name is None:
        manifest = _manifest(json_output)
        application_id, alias = manifest["name"], manifest["server"]
    else:
        try:
            application_id = validate_target_application(app_name)
        except ManifestError as error:
            _fail(str(error), 2, json_output)
        alias, target = _select_server(server_alias, None, json_output)
    if not yes:
        _fail(f"this removes the deployment access of {application_id} on {alias}; rerun with --yes to confirm",
              2, json_output)

    def action() -> tuple[int, dict]:
        admin = _admin(manifest, json_output) if manifest is not None else _admin_target(target, json_output)
        return commands.deprovision_application(application_id, admin, retire=retire)
    _execute(action, json_output, lambda code, result: output.render_deprovision(
        result, f"{application_id} on {alias}"))


@env_app.command("clear")
def env_clear(json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove the application's runtime environment file."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.set_environment(manifest, _admin(manifest, json_output), "clear"), json_output,
             lambda code, result: output.render_environment(
                 result, f"{manifest['name']} on {manifest['server']}", "clear"))


def _ask_tailscale(instructions: dict) -> tuple[str, str] | None:
    """Show the filled-in Tailscale steps and read the two identifiers (not secrets) from the terminal."""
    typer.echo("Tailscale is not set up for CI yet. Set TAILSCALE_API_KEY and rerun to have Shiplord create the trust "
               "credential, or enter the values below yourself:\n")
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


@registry_app.command("login", rich_help_panel="Administration")
def registry_login_command(username: str = typer.Option(..., "--username"),
                           token_file: Optional[Path] = typer.Option(None, "--token-file", help="File holding the token."),
                           token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the token from stdin."),
                           registry: str = typer.Option("ghcr.io", "--registry",
                                                        help="Registry host: ghcr.io (default) or docker.io."),
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
        return commands.registry_login(manifest, _admin(manifest, json_output), registry=registry, username=username, token=token)
    _execute(action, json_output, lambda code, result: output.render_registry(
        result, f"{manifest['name']} on {manifest['server']}", "login"))


@registry_app.command("logout", rich_help_panel="Administration")
def registry_logout_command(registry: str = typer.Option("ghcr.io", "--registry",
                                                         help="Registry host: ghcr.io (default) or docker.io."),
                            json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove this application's registry login from the server."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.registry_other(manifest, _admin(manifest, json_output), "logout", registry), json_output,
             lambda code, result: output.render_registry(
                 result, f"{manifest['name']} on {manifest['server']}", "logout"))


@registry_app.command("status", rich_help_panel="Administration")
def registry_status_command(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show which registries have a login (usernames only; tokens are never shown)."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.registry_other(manifest, _admin(manifest, json_output), "list"), json_output,
             lambda code, result: output.render_registry(
                 result, f"{manifest['name']} on {manifest['server']}", "list"))


def _key_label(label: str) -> str:
    try:
        return validate_label(label)
    except KeyError_ as error:
        raise CommandError(str(error)) from None


def _read_public_key(public_key_file: Optional[Path], public_key_stdin: bool) -> tuple[str, str]:
    """One public key from exactly one source: returns (key line without a comment, fingerprint)."""
    if public_key_stdin == (public_key_file is not None):
        raise CommandError("provide the public key with exactly one of --public-key-file and --public-key-stdin")
    try:
        text = sys.stdin.read() if public_key_stdin else public_key_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CommandError("could not read the public key") from None
    try:
        key = parse_public_key(text)
    except KeyError_ as error:
        raise CommandError(str(error)) from None
    return f"{key.type} {key.blob}", key.fingerprint


@keys_app.command("add")
def keys_add_command(label: str = typer.Option(..., "--label", help="A short name for this key."),
                     public_key_file: Optional[Path] = typer.Option(None, "--public-key-file", help="File holding the public key."),
                     public_key_stdin: bool = typer.Option(False, "--public-key-stdin", help="Read the public key from stdin."),
                     json_output: bool = typer.Option(False, "--json")) -> None:
    """Register a public key that may deploy this application, without needing a template."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        name = _key_label(label)
        public_key, fingerprint = _read_public_key(public_key_file, public_key_stdin)
        result = _admin(manifest, json_output).call("keys", manifest["name"],
                                                    {"operation": "add", "label": name, "public_key": public_key})
        return 0, {"ok": True, **result, "label": name, "fingerprint": fingerprint}
    _execute(action, json_output, lambda code, result: output.render_keys(
        result, f"{manifest['name']} on {manifest['server']}", "add"))


@keys_app.command("list")
def keys_list_command(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the labels and fingerprints of the keys that may deploy this application."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        listed = _admin(manifest, json_output).call("keys", manifest["name"], {"operation": "list"})
        return 0, {"ok": True, **listed}
    _execute(action, json_output, lambda code, result: output.render_keys(
        result, f"{manifest['name']} on {manifest['server']}", "list"))


@keys_app.command("remove")
def keys_remove_command(label: str = typer.Option(..., "--label", help="The label of the key to revoke."),
                        yes: bool = typer.Option(False, "--yes", help="Confirm that this key loses deploy access."),
                        json_output: bool = typer.Option(False, "--json")) -> None:
    """Revoke one key's deploy access. The server refuses to remove the last key."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        name = _key_label(label)
        if not yes:
            raise CommandError(f"this removes {name}'s deploy access to {manifest['name']}; rerun with --yes to confirm")
        result = _admin(manifest, json_output).call("keys", manifest["name"], {"operation": "remove", "label": name})
        return 0, {"ok": True, **result, "label": name}
    _execute(action, json_output, lambda code, result: output.render_keys(
        result, f"{manifest['name']} on {manifest['server']}", "remove"))


def _github_setup(manifest: dict, repository: Optional[str], *, json_output: bool, interactive: bool | None = None,
                  ci_host: Optional[str] = None, tooling_ref: Optional[str] = None, platforms: str = "linux/amd64",
                  tailscale_client_id: Optional[str] = None, tailscale_audience: Optional[str] = None,
                  tailscale_tags: Optional[str] = None, deployment_branch: Optional[str] = None,
                  quality_workflow: Optional[str] = None, no_quality_workflow: bool = False,
                  force_workflow: bool = False, upgrade_workflow: bool = False,
                  build_command: Optional[str] = None, binary_path: Optional[str] = None) -> tuple[int, dict]:
    """Create the CI key, secret, variables, and workflow for an application. Shared by `github setup` and `setup`."""
    if quality_workflow is not None and no_quality_workflow:
        raise CommandError("choose either --quality-workflow or --no-quality-workflow")
    if upgrade_workflow:  # refresh the pins only; no key, secret, variable, or Tailscale call
        if repository is not None:
            github.validate_setup_request(manifest, repository)
        return 0, github.upgrade_workflow(manifest, project=Path.cwd())
    if repository is None:
        raise CommandError("--repo owner/repository is required unless --upgrade-workflow is given")
    github.validate_setup_request(manifest, repository)
    branch = github.resolve_deployment_branch(manifest, deployment_branch)  # before asking for anything
    given = _tailscale_settings(tailscale_client_id, tailscale_audience, tailscale_tags)
    gh = github.GitHub()
    if interactive is None:
        interactive = sys.stdin.isatty() and not json_output
    tags = [tag for tag in given.get("SHIPLORD_TS_TAGS", "").split(",") if tag]
    target = load_server(manifest["server"])  # fail before creating a Tailscale credential
    try:
        settings, origin = tailscale.resolve(
            given, ask=_ask_tailscale if interactive else None,
            guide=lambda: github.tailscale_guidance(gh, repository, manifest, branch),
            provision=lambda: github.tailscale_credential(gh, repository, manifest, tags=tags))
    except tailscale.TailscaleError as error:
        raise CommandError(str(error)) from None
    result = github.github_setup(manifest, repository, project=Path.cwd(), admin=_admin_target(target, json_output), gh=gh,
                                 admin_target=target, ci_host=ci_host, tooling_ref=tooling_ref, platforms=platforms,
                                 build_command=build_command, binary_path=binary_path,
                                 tailscale=settings, deployment_branch=branch, force_workflow=force_workflow,
                                 quality_workflow=quality_workflow, include_quality=not no_quality_workflow)
    result["tailscale"] = origin
    return 0, result


@github_app.command("setup", rich_help_panel="Administration")
def github_setup_command(repo: Optional[str] = typer.Option(None, "--repo", help="owner/repository"),
                         ci_host: Optional[str] = typer.Option(None, "--ci-host", help="Server name CI should use (default: the alias host)."),
                         tooling_ref: Optional[str] = typer.Option(None, "--tooling-ref", help="Commit of Shiplord to pin in CI (default: the installed PyPI version)."),
                         platforms: str = typer.Option("linux/amd64", "--platforms"),
                         build_command: Optional[str] = typer.Option(None, "--build-command",
                                                                     help="Native only: the command CI runs to build the binary."),
                         binary_path: Optional[str] = typer.Option(None, "--binary-path",
                                                                   help="Native only: the path that command writes the binary to."),
                         deployment_branch: Optional[str] = typer.Option(None, "--deployment-branch",
                                                                        help="Branch releases deploy from (default: the manifest's, or main)."),
                         tailscale_client_id: Optional[str] = typer.Option(None, "--tailscale-client-id"),
                         tailscale_audience: Optional[str] = typer.Option(None, "--tailscale-audience"),
                         tailscale_tags: Optional[str] = typer.Option(None, "--tailscale-tags"),
                         quality_workflow: Optional[str] = typer.Option(None, "--quality-workflow",
                                                                        help="Reusable workflow to run before deploy."),
                         no_quality_workflow: bool = typer.Option(False, "--no-quality-workflow",
                                                                  help="Do not run a quality job before deploy."),
                         force_workflow: bool = typer.Option(False, "--force-workflow"),
                         upgrade_workflow: bool = typer.Option(False, "--upgrade-workflow",
                                                               help="Only refresh the action pins in the generated "
                                                                    "workflows; the other options are ignored."),
                         json_output: bool = typer.Option(False, "--json")) -> None:
    """Create the CI key, store it in GitHub, set the repository variables, and write the release workflow.

    With --upgrade-workflow, rewrite only the action pins in the workflows Shiplord already generated.
    With TAILSCALE_API_KEY set, also create the Tailscale federated trust credential; otherwise print the steps.
    """
    manifest = _manifest(json_output)
    # The pin refresh returns a different shape, so it gets the generic renderer rather than "CI configured".
    renderer = None if upgrade_workflow else (
        lambda code, result: output.render_github_setup(result, f"{manifest['name']} on {manifest['server']}"))
    _execute(lambda: _github_setup(manifest, repo, json_output=json_output, ci_host=ci_host, tooling_ref=tooling_ref,
                                   platforms=platforms, tailscale_client_id=tailscale_client_id,
                                   tailscale_audience=tailscale_audience, tailscale_tags=tailscale_tags,
                                   deployment_branch=deployment_branch, quality_workflow=quality_workflow,
                                   no_quality_workflow=no_quality_workflow, force_workflow=force_workflow,
                                   build_command=build_command, binary_path=binary_path,
                                   upgrade_workflow=upgrade_workflow), json_output, renderer)


@app.command(rich_help_panel="Setup")
def setup(server: Optional[str] = typer.Option(None, "--server", help="Registered server alias; only when ship.toml is missing."),
          api_domain: Optional[str] = typer.Option(None, "--api-domain", help="Public hostname; only when ship.toml is missing."),
          frontend: Optional[list[str]] = typer.Option(None, "--frontend"),
          no_frontend: bool = typer.Option(False, "--no-frontend"),
          name: Optional[str] = typer.Option(None, "--name"),
          runtime: Optional[str] = typer.Option(None, "--runtime", help="docker (default) or native."),
          template: Optional[str] = typer.Option(None, "--template", help="google-adk (default) or generic."),
          container_port: Optional[int] = typer.Option(None, "--container-port"),
          health_path: Optional[str] = typer.Option(None, "--health-path"),
          memory: Optional[int] = typer.Option(None, "--memory", help="Memory limit in MiB."),
          public_key: Optional[Path] = typer.Option(None, "--public-key", help="Public key to register."),
          image_repository: Optional[str] = typer.Option(None, "--image-repository"),
          max_memory_mb: Optional[int] = typer.Option(None, "--max-memory-mb"),
          env_file: Optional[Path] = typer.Option(None, "--env-file",
                                                  help="Runtime environment file; omitted leaves the environment unchanged."),
          registry: str = typer.Option("ghcr.io", "--registry"),
          registry_username: Optional[str] = typer.Option(None, "--registry-username"),
          token_file: Optional[Path] = typer.Option(None, "--token-file"),
          token_stdin: bool = typer.Option(False, "--token-stdin"),
          repo: Optional[str] = typer.Option(None, "--repo", help="owner/repository; default: ship.toml's [github]."),
          ci_host: Optional[str] = typer.Option(None, "--ci-host"),
          tooling_ref: Optional[str] = typer.Option(None, "--tooling-ref"),
          platforms: str = typer.Option("linux/amd64", "--platforms"),
          tailscale_client_id: Optional[str] = typer.Option(None, "--tailscale-client-id"),
          tailscale_audience: Optional[str] = typer.Option(None, "--tailscale-audience"),
          tailscale_tags: Optional[str] = typer.Option(None, "--tailscale-tags"),
          force_workflow: bool = typer.Option(False, "--force-workflow"),
          ci_timeout: int = typer.Option(1200, "--ci-timeout", help="Seconds to wait for the final check."),
          skip_ci: bool = typer.Option(False, "--skip-ci", help="Stop after the local steps; CI is not configured or checked."),
          rerun: bool = typer.Option(False, "--rerun", help="Repeat the repeatable steps; ship.toml is never replaced."),
          json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Run the first-run steps in order and resume at the first incomplete one.

    The steps are configure, provision, env, registry login, github setup, and check-ci.
    Completed steps are observed, not remembered, so an interrupted run continues where it stopped."""
    if frontend and no_frontend:
        _fail("specify --frontend or explicitly choose --no-frontend", 2, json_output)
    directory = Path.cwd()
    existing = None
    if (directory / "ship.toml").exists() or (directory / "ship.toml").is_symlink():
        try:
            existing = load_manifest()
        except ManifestError as error:
            _fail(str(error), 2, json_output)
    repository = repo or (existing or {}).get("github", {}).get("repository")
    if existing is not None and "github" in existing and repo and repo != existing["github"]["repository"]:
        _fail(f"ship.toml already names {existing['github']['repository']}; edit it first to change repositories",
              2, json_output)
    template_hint = existing["template"] if existing is not None else (template or "google-adk")
    runtime_hint = existing["runtime"] if existing is not None else (runtime or "docker")
    if not skip_ci:
        if repository is None:
            _fail("pass --repo owner/repository; CI setup and the final check need a GitHub repository "
                  "(or pass --skip-ci)", 2, json_output)
        try:
            if existing is not None:
                github.validate_setup_request(existing, repository)
            else:
                github.validate_repository(repository)
        except github.GitHubError as error:
            _fail(str(error), error.code, json_output)
        if template_hint == "generic":
            _fail("--template generic needs --skip-ci; github setup generates an ADK workflow", 2, json_output)
        if runtime_hint != "docker" and not (directory / github.WORKFLOW_PATH).is_file():
            recorded = (existing or {}).get("github") or {}
            if not (recorded.get("build_command") and recorded.get("binary_path")):
                _fail("--runtime native needs github.build_command and github.binary_path in ship.toml, a "
                      f"workflow at {github.WORKFLOW_PATH}, or --skip-ci; `shiplord github setup "
                      "--build-command ... --binary-path ...` records both", 2, json_output)
    provision_image = image_repository or (f"ghcr.io/{repository.lower()}" if repository else None)
    if runtime_hint == "docker" and provision_image is None:
        _fail("pass --repo owner/repository or --image-repository so provision knows the image repository",
              2, json_output)
    if env_file is not None:
        if not env_file.is_file():
            _fail("the environment file was not found", 2, json_output)
        try:
            values = parse_environment(env_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            _fail("could not read the environment file", 2, json_output)
        except EnvironmentError_ as error:
            _fail(str(error), 2, json_output)
        if not values:
            _fail("the environment file defines no variables", 2, json_output)
    if public_key is not None:
        try:
            commands.read_public_key(public_key)
        except CommandError as error:
            _fail(str(error), error.code, json_output)
    token = None
    if registry_username is None:
        if token_file is not None or token_stdin:
            _fail("--token-file and --token-stdin require --registry-username", 2, json_output)
    else:
        if token_stdin == (token_file is not None):
            _fail("provide the token with exactly one of --token-file and --token-stdin", 2, json_output)
        try:
            token = (sys.stdin.read() if token_stdin else token_file.read_text(encoding="utf-8")).strip()
        except (OSError, UnicodeDecodeError):
            _fail("could not read the token", 2, json_output)
        try:
            commands.validate_registry_login(registry, registry_username, token)
        except CommandError as error:
            _fail(str(error), error.code, json_output)

    # stdin carries the registry token when --token-stdin is used, so configure must not prompt from it too.
    interactive = _interactive() and not json_output and not token_stdin
    given = {"server": server, "api_domain": api_domain, "name": name, "runtime": runtime, "template": template,
             "origins": list(frontend) if frontend else ([] if no_frontend else None),
             "container_port": container_port, "readiness_path": health_path, "memory_mb": memory}
    state: dict = {"manifest": existing, "admin": None}

    def manifest() -> dict:
        if state["manifest"] is None:
            state["manifest"] = load_manifest()
        return state["manifest"]

    def admin() -> AdminClient:
        if state["admin"] is None:
            state["admin"] = _admin(manifest(), json_output)
        return state["admin"]

    def run_github(current: dict) -> tuple[int, dict]:
        code, result = _github_setup(current, repository, json_output=json_output, interactive=interactive,
                                     ci_host=ci_host, tooling_ref=tooling_ref, platforms=platforms,
                                     tailscale_client_id=tailscale_client_id, tailscale_audience=tailscale_audience,
                                     tailscale_tags=tailscale_tags, force_workflow=force_workflow)
        state["manifest"] = None  # github setup writes [github] into ship.toml
        return code, result

    steps = [
        setup_module.configure_step(directory, given, interactive=interactive,
                                    servers=lambda: list_servers()[0] if server is None else [],
                                    ask=_ask, say=typer.echo, confirm=_confirm_manifest),
        setup_module.provision_step(manifest, admin, public_key=public_key, image_repository=provision_image,
                                    max_memory_mb=max_memory_mb),
        setup_module.environment_step(manifest, admin, env_file),
        setup_module.registry_step(manifest, admin, registry=registry, username=registry_username, token=token),
    ]
    if skip_ci:
        steps.append(setup_module.Step("github", run=lambda: (0, {}),
                                       skip_reason="--skip-ci given; CI was not configured"))
        steps.append(setup_module.Step("check-ci", run=lambda: (0, {}),
                                       skip_reason="--skip-ci given; CI was not checked"))
    else:
        steps.append(setup_module.github_step(
            manifest, run=run_github,
            complete=lambda current: github.setup_complete(current, project=directory, repository=repository)))
        steps.append(setup_module.check_ci_step(
            manifest, lambda current: github.check_ci(current, gh=github.GitHub(), timeout=ci_timeout)))

    def report(index: int, total: int, step: str, status: str) -> None:
        if not json_output and status == "running":
            typer.echo(f"Step {index}/{total} {step}: running", err=True)

    def action() -> tuple[int, dict]:
        code, result = setup_module.run_setup(steps, rerun=rerun, report=report)
        if not result["ok"] and result.get("error"):
            result["error"] = _recovery_hint(result["error"])
        try:
            current = manifest()
            result = {"application": current["name"], "server": current["server"], **result}
        except ManifestError:
            pass
        return code, result
    _execute(action, json_output, lambda code, result: output.render_setup(result))


@github_app.command("check-ci", rich_help_panel="Administration")
def github_check_ci_command(timeout: int = typer.Option(1200, "--timeout", help="Seconds to wait for the run."),
                            json_output: bool = typer.Option(False, "--json")) -> None:
    """Run the release workflow in check-only mode: join Tailscale, log in with the CI key, deploy nothing."""
    manifest = _manifest(json_output)
    _execute(lambda: github.check_ci(manifest, gh=github.GitHub(), timeout=timeout), json_output,
             lambda code, result: output.render_github_check_ci(
                 result, f"{manifest['name']} on {manifest['server']}"))


@github_app.command("check-pins", rich_help_panel="Administration")
def github_check_pins_command(json_output: bool = typer.Option(False, "--json")) -> None:
    """Confirm every action Shiplord pins still resolves in its own repository.

    Needs the GitHub API: a token is read from GITHUB_TOKEN or GH_TOKEN, never from an argument. A pin the API
    cannot be asked about is reported as unverified, never as resolving.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    _execute(lambda: workflow.check_action_pins(token=token), json_output)


@github_app.command("status", rich_help_panel="Administration")
def github_status_command(check_server: bool = typer.Option(False, "--check-server", help="Also confirm the key on the server."),
                          json_output: bool = typer.Option(False, "--json")) -> None:
    """Check the repository access, secret, variables, workflow, CI key, and the last workflow run."""
    manifest = _manifest(json_output)
    _execute(lambda: github.github_status(manifest, project=Path.cwd(), gh=github.GitHub(),
                                          admin=_admin(manifest, json_output) if check_server else None), json_output,
             lambda code, result: output.render_github_status(
                 result, f"{manifest['name']} on {manifest['server']}"))


@github_app.command("rotate-key", rich_help_panel="Administration")
def github_rotate_key(json_output: bool = typer.Option(False, "--json")) -> None:
    """Replace the CI key: add and prove the new one, update GitHub, then revoke the old one."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        target = load_server(manifest["server"])
        return 0, github.rotate_key(manifest, admin=_admin_target(target, json_output), gh=github.GitHub(), admin_target=target)
    _execute(action, json_output, lambda code, result: output.render_github_rotate_key(
        result, f"{manifest['name']} on {manifest['server']}"))


def _order_help() -> None:
    panel_order = {"Quick start": 0, "Setup": 1, "Troubleshooting": 2, "Administration": 3}
    quick_order = {name: index for index, name in enumerate(("apps", "status", "logs", "plan", "deploy"))}
    app.registered_commands.sort(key=lambda info: (
        panel_order.get(info.rich_help_panel, 4),
        quick_order.get(info.name or info.callback.__name__, 99),
        info.name or info.callback.__name__,
    ))
    app.registered_groups.sort(key=lambda info: (panel_order.get(info.rich_help_panel, 4), info.name or ""))


_order_help()


def main() -> None:
    app()
