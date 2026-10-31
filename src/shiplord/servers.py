"""Register servers after checking existing OpenSSH trust and login."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .manifest import validate_app_name
from .protocol import _invalid_constant, _unique_keys


class ServerError(ValueError):
    """Server registration could not be completed."""


class InvalidTarget(ServerError):
    """The SSH target has invalid syntax."""


class SSHCheckError(ServerError):
    """A trusted SSH login could not be verified."""


class NoServerDirectory(ServerError):
    """No server has been registered on this machine yet (the directory does not exist)."""


def validate_target(target: str) -> str:
    if not isinstance(target, str) or target.count("@") != 1:
        raise InvalidTarget("SSH target must be user@host")
    user, host = target.split("@")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or not host or len(host) > 253:
        raise InvalidTarget("SSH target must be user@host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,62}", label) for label in host.split(".")):
            raise InvalidTarget("SSH target must contain a valid host or SSH alias") from None
    if "%" in host:
        raise InvalidTarget("scoped IP addresses are not supported")
    return target


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    path = Path(value) if value else Path.home() / ".config"
    if not path.is_absolute():
        raise ServerError("XDG_CONFIG_HOME must be an absolute path")
    return path


def _private_servers_directory(*, create: bool = True) -> int:
    """Open the private server directory; the caller owns the returned fd."""
    base = _config_home()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        if create:
            base.mkdir(parents=True, exist_ok=True)
        current = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in ("shiplord", "servers"):
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                child = os.open(name, flags, dir_fd=current)
                os.close(current)
                current = child
                details = os.fstat(current)
                if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
                    raise ServerError("Shiplord's configuration directories must be private and user-owned")
            return current
        except BaseException:
            os.close(current)
            raise
    except FileNotFoundError:
        if not create:
            raise NoServerDirectory("could not access the private server configuration directory") from None
        raise ServerError("could not access the private server configuration directory") from None
    except OSError:
        raise ServerError("could not access the private server configuration directory") from None


def _read_record(directory: int, alias: str) -> str:
    """Read and validate one alias file inside the already opened private directory."""
    try:
        descriptor = os.open(alias + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as file:
            details = os.fstat(file.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                    or stat.S_IMODE(details.st_mode) & 0o077 or details.st_size > 4096):
                raise ServerError("server alias file is not a private regular file")
            data = file.read(4097)
        if len(data) > 4096:
            raise ServerError("server alias file is too large")
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
        if (not isinstance(record, dict) or set(record) != {"schema_version", "alias", "ssh"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["alias"] != alias):
            raise ServerError("server alias file is invalid")
        return validate_target(record["ssh"])
    except (OSError, UnicodeDecodeError, ValueError):
        raise ServerError("could not read a valid private server alias") from None


def load_server(alias: str) -> str:
    """Read a previously registered alias without creating local state."""
    alias = validate_app_name(alias)
    directory = _private_servers_directory(create=False)
    try:
        return _read_record(directory, alias)
    finally:
        os.close(directory)


def list_servers() -> tuple[list[dict[str, str]], int]:
    """Return the registered aliases (sorted) and how many entries could not be read, without creating local state.

    Only files named ``<valid alias>.json`` are candidates, so a hostile file name is never echoed to a terminal; any
    other entry, and any candidate that fails the same checks as ``load_server``, is only counted.
    """
    try:
        directory = _private_servers_directory(create=False)
    except NoServerDirectory:
        return [], 0
    servers, invalid = [], 0
    try:
        try:
            with os.scandir(directory) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            raise ServerError("could not list the private server configuration directory") from None
        for name in names:
            if name.startswith(".new-"):  # an unpublished temporary file from an interrupted `server add`
                continue
            alias = name[:-len(".json")] if name.endswith(".json") else None
            try:
                validate_app_name(alias)
                servers.append({"alias": alias, "ssh": _read_record(directory, alias)})
            except ValueError:  # ServerError and ManifestError are both ValueErrors
                invalid += 1
    finally:
        os.close(directory)
    return servers, invalid


def _save_server(alias: str, target: str) -> None:
    directory = _private_servers_directory()
    temporary = ".new-" + secrets.token_hex(16)
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({"schema_version": 1, "alias": alias, "ssh": target}, file)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, alias + ".json", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    except FileExistsError:
        raise ServerError("server alias already exists; no file was replaced") from None
    except OSError:
        raise ServerError("could not save the server alias") from None
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)


@dataclass(frozen=True)
class SshOptions:
    """Connection settings that do not come from the user's OpenSSH configuration (used by CI)."""

    identity: Path | None = None
    known_hosts: Path | None = None
    port: int | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "SshOptions":
        # An empty value (an unset GitHub variable expands to "") means "not set".
        identity, known_hosts, port = (environ.get("SHIPLORD_SSH_IDENTITY") or None,
                                       environ.get("SHIPLORD_SSH_KNOWN_HOSTS_FILE") or None,
                                       environ.get("SHIPLORD_SSH_PORT") or None)
        for name, value in (("SHIPLORD_SSH_IDENTITY", identity), ("SHIPLORD_SSH_KNOWN_HOSTS_FILE", known_hosts)):
            if value is not None and (not os.path.isabs(value) or not os.path.isfile(value)):
                raise ServerError(f"{name} must be the absolute path of an existing file")
        if port is not None and not (port.isdigit() and 1 <= int(port) <= 65535):
            raise ServerError("SHIPLORD_SSH_PORT must be a port number")
        return cls(Path(identity) if identity else None, Path(known_hosts) if known_hosts else None,
                   int(port) if port else None)


def ssh_command(target: str, remote_command: str, options: SshOptions | None = None) -> list[str]:
    """Build a fresh, host-key-checked SSH invocation from a validated target."""
    target = validate_target(target)
    settings = [
        "BatchMode=yes", "StrictHostKeyChecking=yes", "VerifyHostKeyDNS=no",
        "UpdateHostKeys=no", "ControlMaster=no", "ControlPath=none",
        "ClearAllForwardings=yes", "ForwardAgent=no", "RequestTTY=no",
        "ConnectTimeout=10", "ConnectionAttempts=1",
    ]
    command = ["ssh"]
    if options is not None:
        if options.identity and options.known_hosts:
            # Pinned mode (CI, and the setup check that proves the CI key): ignore the user's ~/.ssh/config, which would
            # otherwise add the personal identity, other known-hosts files, and proxies behind the pinned settings.
            command.extend(["-F", "/dev/null"])
        if options.identity:
            command.extend(["-i", str(options.identity)])
            settings.append("IdentitiesOnly=yes")
        if options.known_hosts:  # the pinned file is the only trust source; no system or user fallback
            settings.extend([f"UserKnownHostsFile={options.known_hosts}", "GlobalKnownHostsFile=/dev/null"])
        if options.port:
            command.extend(["-p", str(options.port)])
    for option in settings:
        command.extend(["-o", option])
    command.extend(["--", target, remote_command])
    return command


def register_server(alias: str, target: str) -> None:
    alias = validate_app_name(alias)
    target = validate_target(target)
    command = ssh_command(target, "true")
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SSHCheckError("SSH check could not be completed; verify OpenSSH and your connection") from None
    if result.returncode != 0:
        raise SSHCheckError("SSH check failed; verify host trust, login, and any locked SSH key")
    _save_server(alias, target)
