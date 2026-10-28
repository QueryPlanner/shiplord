"""Idempotent application provisioning, run by root on behalf of an administrator.

Each step can be repeated. The registration is written last, because it is what
turns on the application's access through the privileged helper; a failure part
way leaves an inert application that a repeat run completes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable

from .environment import EnvironmentError_, load_environment_file, parse_environment
from .keys import KeyError_, KeyStore, PublicKey, parse_public_key, validate_label
from .layout import Layout, deployment_user, runtime_user
from .manifest import validate_app_name
from .registry import DEFAULT_MAX_MEMORY_MB, PORT_RANGE, Registry, validate_registration
from .release_state import HostLock, ReleaseState, StateError
from .system import CommandError, System

USERADD, USERMOD, USERDEL = "/usr/sbin/useradd", "/usr/sbin/usermod", "/usr/sbin/userdel"
GETENT, VISUDO = "/usr/bin/getent", "/usr/sbin/visudo"
SUDOERS_RULE = '{user} ALL=(root) NOPASSWD: NOSETENV: {helper} ""\n'


class ProvisionError(Exception):
    """Provisioning failed; the message names the step."""


def _marker(application_id: str) -> str:
    """The account comment that marks an account as ours; passwd comments cannot contain a colon."""
    return f"shiplord-deploy-{application_id}"


def _run(system: System, argv: list[str], step: str):
    try:
        return system.run(argv)
    except CommandError:
        raise ProvisionError(f"could not run {step}") from None


def _account(system: System, name: str) -> dict | None:
    result = _run(system, [GETENT, "passwd", name], "getent")
    if result.code == 2:
        return None
    fields = result.output.strip().split(":")
    if result.code != 0 or len(fields) != 7:
        raise ProvisionError("could not look up the deployment account")
    return {"name": fields[0], "uid": int(fields[2]), "comment": fields[4], "home": fields[5], "shell": fields[6]}


def _ensure_user(system: System, name: str, marker: str, home: str, shell: str) -> tuple[dict, bool]:
    """Create a system account, or verify that an existing one is ours; never adopt a foreign account."""
    account = _account(system, name)
    created = False
    if account is None:
        for argv, step in (([USERADD, "--system", "--user-group", "--no-create-home", "--home-dir", home,
                             "--shell", shell, "--comment", marker, name], "useradd"),
                           ([USERMOD, "--password", "*", name], "usermod")):
            if _run(system, argv, step).code != 0:
                raise ProvisionError(f"{step} failed for the account {name}")
        account, created = _account(system, name), True
    if (account is None or account["comment"] != marker or account["home"] != home
            or account["shell"] != shell or account["uid"] < 1):
        raise ProvisionError("an account with that name exists but is not a Shiplord account")
    return account, created


def _ensure_account(system: System, application_id: str) -> tuple[dict, bool]:
    return _ensure_user(system, deployment_user(application_id), _marker(application_id), "/", "/bin/sh")


def _ensure_runtime(layout: Layout, system: System, application_id: str) -> dict[str, bool]:
    """A native application runs as its own unprivileged account, with writable data outside releases."""
    account, created = _ensure_user(system, runtime_user(application_id), f"shiplord-runtime-{application_id}",
                                    "/nonexistent", "/usr/sbin/nologin")
    data = layout.data / application_id
    made = not data.exists()
    try:
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        if data.is_symlink():
            raise ProvisionError(f"{data} is a symbolic link")
        os.chmod(data, 0o700)
        if os.geteuid() == 0:
            os.chown(data, account["uid"], int(_run(system, [GETENT, "passwd", account["name"]], "getent")
                                              .output.split(":")[3]))
        (layout.releases / application_id).mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chmod(layout.releases / application_id, 0o755)
    except OSError:
        raise ProvisionError("could not prepare the application's data and release directories") from None
    return {"runtime_account": created, "data_directory": made}


def _private_directory(path: Path) -> bool:
    created = not path.exists()
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(path)
        if path.is_symlink() or info.st_uid != os.geteuid():
            raise ProvisionError(f"{path} is not a directory owned by the installing account")
        os.chmod(path, 0o700)
    except OSError:
        raise ProvisionError(f"could not prepare {path}") from None
    return created


def _install_sudoers(layout: Layout, system: System, application_id: str, account: str) -> bool:
    target = layout.sudoers_dir / f"shiplord-{application_id}"
    desired = SUDOERS_RULE.format(user=account, helper=layout.host)
    if target.exists() and not target.is_symlink() and target.read_text() == desired:
        return False
    # A name containing a dot is never loaded by sudo, so an interrupted write is inert.
    temporary = target.with_name(f"shiplord.{application_id}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440)
        with os.fdopen(descriptor, "w") as file:
            file.write(desired)
            file.flush()
            os.fchmod(file.fileno(), 0o440)
            if os.geteuid() == 0:
                os.fchown(file.fileno(), 0, 0)
            os.fsync(file.fileno())
        if _run(system, [VISUDO, "-cf", str(temporary)], "visudo").code != 0:
            raise ProvisionError("sudo rejected the generated policy")
        os.replace(temporary, target)
    except OSError:
        raise ProvisionError("could not install the sudo policy") from None
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ProvisionError(f"{path} is unavailable; has the server been bootstrapped?") from None


def provision(layout: Layout, system: System, lock: HostLock, *, application_id: str, runtime: str, api_domain: str,
              image_repository: str | None, public_key: str, key_label: str,
              max_memory_mb: int = DEFAULT_MAX_MEMORY_MB) -> dict:
    """Create or complete an application's host resources. The caller holds the host lock."""
    if not lock.held:
        raise ProvisionError("host lock is required")
    app = validate_app_name(application_id)
    try:
        key = parse_public_key(public_key)
        label = validate_label(key_label)
    except KeyError_ as error:
        raise ProvisionError(str(error)) from None
    try:  # reject bad input before the first side effect; uid and port are placeholders here
        validate_registration({"schema_version": 1, "application_id": app, "deployment_uid": 1,
                               "deployment_user": deployment_user(app), "runtime": runtime,
                               "api_domain": api_domain, "host_port": PORT_RANGE.start,
                               "image_repository": image_repository, "max_memory_mb": max_memory_mb})
    except StateError as error:
        raise ProvisionError(f"invalid application settings: {error}") from None
    created: dict[str, bool] = {}
    account, created["account"] = _ensure_account(system, app)
    if runtime == "native":
        created.update(_ensure_runtime(layout, system, app))
    created["state_directory"] = _private_directory(layout.app_state / app)
    created["environment_directory"] = _private_directory(layout.environments / app)
    state_fd = _open_directory(layout.app_state / app)
    try:
        store = ReleaseState(app, state_fd, lock)
        if not (layout.app_state / app / "state.json").exists():
            store.initialize()
            created["release_state"] = True
        else:
            store.read_state()
            created["release_state"] = False
    except StateError as error:
        raise ProvisionError(f"release state is not usable: {error}") from None
    finally:
        os.close(state_fd)
    try:
        created["key"] = KeyStore(layout.authorized_keys, app, account["name"]).add(label, key)
    except KeyError_ as error:
        raise ProvisionError(str(error)) from None
    created["sudo_policy"] = _install_sudoers(layout, system, app, account["name"])
    registry_fd = _open_directory(layout.registrations)
    try:
        registry = Registry(registry_fd, lock)
        before = registry.read(app)
        record = registry.register(app, deployment_uid=account["uid"], deployment_user=account["name"],
                                   runtime=runtime, api_domain=api_domain, image_repository=image_repository,
                                   max_memory_mb=max_memory_mb)
        created["registration"] = before is None
    except StateError as error:
        raise ProvisionError(f"registration failed: {error}") from None
    finally:
        os.close(registry_fd)
    return {"application_id": app, "deployment_user": account["name"], "host_port": record["host_port"],
            "api_domain": record["api_domain"], "runtime": record["runtime"], "max_memory_mb": record["max_memory_mb"],
            "key_fingerprint": key.fingerprint,
            "created": created}


def manage_key(layout: Layout, application_id: str, operation: str, *, label: str | None = None,
               public_key: str | None = None) -> dict:
    """Add, remove, or list keys for a provisioned application."""
    app = validate_app_name(application_id)
    store = KeyStore(layout.authorized_keys, app, deployment_user(app))
    try:
        if operation == "list":
            return {"keys": [{"label": name, "fingerprint": fingerprint} for name, fingerprint in store.list()]}
        label = validate_label(label)
        if operation == "add":
            return {"changed": store.add(label, parse_public_key(public_key))}
        if operation == "remove":
            if len(store.list()) == 1 and store.list()[0][0] == label:
                raise ProvisionError("refusing to remove the last key; add the replacement first")
            return {"changed": store.remove(label)}
    except KeyError_ as error:
        raise ProvisionError(str(error)) from None
    raise ProvisionError("unknown key operation")


def manage_environment(layout: Layout, application_id: str, operation: str, *, text: str | None = None) -> dict:
    """Set, clear, or list the key names of an application's runtime environment file.

    Values are never returned. They take effect the next time a release is started.
    """
    app = validate_app_name(application_id)
    directory = layout.environments / app
    if not directory.is_dir() or directory.is_symlink():
        raise ProvisionError("the application is not provisioned")
    path = directory / "app.env"
    try:
        current = load_environment_file(path) or {}
        if operation == "keys":
            return {"keys": sorted(current)}
        if operation == "clear":
            existed = path.exists()
            path.unlink(missing_ok=True)
            return {"keys": [], "changed": existed}
        if operation != "set":
            raise ProvisionError("unknown environment operation")
        if not isinstance(text, str):
            raise ProvisionError("environment text is required")
        values = parse_environment(text)
    except EnvironmentError_ as error:
        raise ProvisionError(str(error)) from None
    if values == current and path.exists():
        return {"keys": sorted(values), "changed": False}
    temporary = path.with_name(f".app.env.tmp-{secrets.token_hex(6)}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write("".join(f"{key}={values[key]}\n" for key in sorted(values)))
            file.flush()
            os.fchmod(file.fileno(), 0o600)
            if os.geteuid() == 0:
                os.fchown(file.fileno(), 0, 0)
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ProvisionError("could not write the environment file") from None
    finally:
        temporary.unlink(missing_ok=True)
    return {"keys": sorted(values), "changed": True}


REGISTRY_HOSTS = ("ghcr.io",)
_LOGIN_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_LOGIN_TOKEN = re.compile(r"[\x21-\x7e]{1,512}\Z")


def manage_registry(layout: Layout, application_id: str, operation: str, *, registry: str | None = None,
                    username: str | None = None, token: str | None = None) -> dict:
    """Keep one application's read-only registry login in a root-only docker config directory.

    The login is per application, so a token cannot be reused by another application's deployments. Tokens
    are never returned; ``list`` shows registries and usernames only.
    """
    app = validate_app_name(application_id)
    if not (layout.registrations / f"{app}.json").is_file():
        raise ProvisionError("the application is not provisioned")
    directory = layout.registry_logins / app
    path = directory / "config.json"
    try:
        current = json.loads(path.read_text()).get("auths", {}) if path.exists() else {}
    except (OSError, ValueError, AttributeError):
        raise ProvisionError("the registry login file is unreadable; remove and recreate it") from None
    def summary() -> dict:  # registries and usernames only; never a token
        return {"registries": [{"registry": name, "username": base64.b64decode(entry["auth"]).split(b":", 1)[0].decode()}
                               for name, entry in sorted(current.items())]}

    if operation == "list":
        return summary()
    if registry not in REGISTRY_HOSTS:
        raise ProvisionError(f"registry logins are accepted for {', '.join(REGISTRY_HOSTS)} only")
    if operation == "logout":
        current.pop(registry, None)
    elif operation == "login":
        if not isinstance(username, str) or not _LOGIN_USER.fullmatch(username):
            raise ProvisionError("invalid registry username")
        if not isinstance(token, str) or not _LOGIN_TOKEN.fullmatch(token):
            raise ProvisionError("invalid registry token")
        current[registry] = {"auth": base64.b64encode(f"{username}:{token}".encode()).decode()}
    else:
        raise ProvisionError("unknown registry operation")
    try:
        if not current:
            path.unlink(missing_ok=True)
            return summary()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        temporary = directory / f".config.tmp-{secrets.token_hex(6)}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump({"auths": current}, file)
            file.flush()
            if os.geteuid() == 0:
                os.fchown(file.fileno(), 0, 0)
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ProvisionError("could not write the registry login") from None
    return summary()


def deprovision(layout: Layout, system: System, lock: HostLock, application_id: str, *,
                retire: Callable[[dict, dict], None] | None = None) -> dict:
    """Remove an application's access and host resources.

    While a release is deployed this refuses, unless ``retire(registration, state)`` is given: it stops the runtime,
    removes the route and discards the releases first, and any failure of it leaves the application registered.
    """
    if not lock.held:
        raise ProvisionError("host lock is required")
    app = validate_app_name(application_id)
    name = deployment_user(app)
    state_path = layout.app_state / app
    if (state_path / "journal.json").exists():
        raise ProvisionError("an unfinished transaction exists; recover it before removing the application")
    state_file = state_path / "state.json"
    if state_file.exists():
        state_fd = _open_directory(state_path)
        try:
            state = ReleaseState(app, state_fd, lock).read_state()
            if state["current"] is not None:
                if retire is None:
                    raise ProvisionError("a release is deployed; retire it before removing the application")
                registry_fd = _open_directory(layout.registrations)
                try:
                    registration = Registry(registry_fd, lock).read(app)
                finally:
                    os.close(registry_fd)
                if registration is None:
                    raise ProvisionError("the application is not registered")
                retire(registration, state)
        except StateError as error:
            raise ProvisionError(f"release state is not usable: {error}") from None
        finally:
            os.close(state_fd)
    removed: dict[str, bool] = {}
    registry_fd = _open_directory(layout.registrations)
    try:
        removed["registration"] = Registry(registry_fd, lock).unregister(app)  # first: disables access
    except StateError as error:
        raise ProvisionError(str(error)) from None
    finally:
        os.close(registry_fd)
    sudoers = layout.sudoers_dir / f"shiplord-{app}"
    removed["sudo_policy"] = sudoers.exists()
    sudoers.unlink(missing_ok=True)
    keys = layout.authorized_keys / name
    removed["keys"] = keys.exists()
    KeyStore(layout.authorized_keys, app, name).delete()
    account = _account(system, name)
    if account is not None:
        if account["comment"] != _marker(app):
            raise ProvisionError("refusing to remove an account that is not a Shiplord deployment account")
        if _run(system, [USERDEL, name], "userdel").code != 0:
            raise ProvisionError("could not remove the deployment account; end its sessions and retry")
    removed["account"] = account is not None
    for directory, key in ((layout.app_state / app, "state_directory"), (layout.environments / app, "environment_directory"),
                           (layout.registry_logins / app, "registry_login")):
        if directory.is_symlink():
            raise ProvisionError(f"{directory} is a symbolic link")
        if directory.exists():
            shutil.rmtree(directory)
            removed[key] = True
        elif key != "registry_login":  # an optional item is reported only when it existed
            removed[key] = False
    retained = []
    if (layout.data / app).exists():
        retained.append("data directory and runtime account (they hold application data)")
    if (layout.releases / app).is_symlink():
        raise ProvisionError(f"{layout.releases / app} is a symbolic link")
    if (layout.releases / app).exists():
        shutil.rmtree(layout.releases / app)
        removed["release_binaries"] = True
    unit = layout.systemd_units / f"shiplord-{app}.service"
    if unit.exists():
        _run(system, ["/usr/bin/systemctl", "disable", "--now", unit.name], "systemctl")
        unit.unlink(missing_ok=True)
        _run(system, ["/usr/bin/systemctl", "daemon-reload"], "systemctl")
        removed["service_unit"] = True
    return {"application_id": app, "removed": removed, "retained": retained}
