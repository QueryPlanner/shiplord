"""Caddy route transactions for registered applications.

Each application owns one file, ``<root>/ship/<app>.caddy``, pulled into the main
Caddyfile by a single top-level import. A candidate route is validated inside a
temporary Caddyfile before it replaces the live file, so the file Caddy would
read after a restart has always been validated. The previous file is kept as a
hard link until the reload succeeds, which makes the backup durable.

Assumptions not verified against a real Caddy yet (a Linux integration job must
confirm them): explicit imports of dot-named files, validation behavior under
the invoking account, and the reload command. Other writers of ``<root>`` are
not excluded by the host lock; validation is the backstop for that race.
"""

from __future__ import annotations

import glob
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from .manifest import validate_app_name, validate_hostname
from .registry import PORT_RANGE, validate_registration
from .release_state import HostLock, _directory
from .system import CommandError, run_command

MAX_CONFIG_BYTES = 1024 * 1024
MAX_IMPORTED_FILES = 64
MAX_IMPORT_DEPTH = 3
COMMAND_TIMEOUT_SECONDS = 30
_TEMP_PREFIX = ".tmp-"
_PRIOR_PREFIX = ".prior-"
_VALIDATE_PREFIX = ".shiplord-validate-"


class RoutingError(Exception):
    """A routing operation failed; ``kind`` classifies the failure."""

    def __init__(self, message: str, kind: str, *, intervention_required: bool = False, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.intervention_required = intervention_required
        self.detail = detail  # capped command output; never part of str(self)


@dataclass(frozen=True)
class RouteResult:
    changed: bool


class CaddyCommands(Protocol):
    def validate(self, config: Path) -> None: ...
    def reload(self) -> None: ...


class SubprocessCaddy:
    """Run Caddy and systemd through fixed executables; no shell, minimal environment."""

    ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}

    def __init__(self, caddy: str = "/usr/bin/caddy", systemctl: str = "/usr/bin/systemctl",
                 timeout: float = COMMAND_TIMEOUT_SECONDS):
        self.caddy, self.systemctl, self.timeout = caddy, systemctl, timeout

    def validate(self, config: Path) -> None:
        self._run([self.caddy, "validate", "--config", str(config), "--adapter", "caddyfile"], "validation")

    def reload(self) -> None:
        self._run([self.systemctl, "reload", "caddy"], "reload")

    def _run(self, argv: list[str], kind: str) -> None:
        try:
            result = run_command(argv, timeout=self.timeout, env=self.ENVIRONMENT)
        except CommandError as error:
            message = "Caddy command timed out" if error.kind == "timeout" else "could not start the Caddy command"
            raise RoutingError(message, kind, detail=error.output) from None
        if result.code != 0:
            raise RoutingError("Caddy command failed", kind, detail=result.output)


def render_route(domain: str, port: int) -> bytes:
    """Render a route from validated values only."""
    domain = validate_hostname(domain, "api_domain")
    if type(port) is not int or port not in PORT_RANGE:
        raise RoutingError("upstream port is outside the managed range", "precondition")
    return f"{domain} {{\n\treverse_proxy 127.0.0.1:{port}\n}}\n".encode()


# --- Caddyfile inspection -------------------------------------------------

def _lines(text: str) -> Iterator[tuple[int, list[str], list[str], int]]:
    """Yield (line number, tokens, structural tokens, depth before the line)."""
    depth = 0
    heredoc: str | None = None
    for number, raw in enumerate(text.splitlines()):
        if heredoc is not None:
            if raw.split(None, 1)[:1] == [heredoc]:  # the marker may be followed by more arguments
                heredoc = None
            continue
        tokens: list[str] = []
        structural: list[str] = []
        i, n = 0, len(raw)
        while i < n:
            char = raw[i]
            if char.isspace():
                i += 1
            elif char == "#":
                break
            elif char in "\"`":
                j, buffer = i + 1, []
                while j < n and raw[j] != char:
                    if char == '"' and raw[j] == "\\" and j + 1 < n:
                        j += 1
                    buffer.append(raw[j])
                    j += 1
                if j >= n:
                    raise RoutingError("cannot inspect the Caddy configuration", "uninspectable")
                tokens.append("".join(buffer))
                i = j + 1
            else:
                j = i
                while j < n and not raw[j].isspace():
                    j += 1
                tokens.append(raw[i:j])
                structural.append(raw[i:j])
                i = j
        if tokens and structural and tokens[-1] == structural[-1] and tokens[-1].startswith("<<") and len(tokens[-1]) > 2:
            heredoc = tokens[-1][2:]
        yield number, tokens, structural, depth
        for token in structural:
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
                if depth < 0:
                    raise RoutingError("cannot inspect the Caddy configuration", "uninspectable")
    if depth != 0 or heredoc is not None:
        raise RoutingError("cannot inspect the Caddy configuration", "uninspectable")


def _addresses(token: str) -> Iterator[str]:
    for part in token.split(","):
        part = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", part.strip()).split("/")[0]
        yield _canonical_host(re.sub(r":[0-9]+$", "", part))


def _canonical_host(host: str) -> str:
    """Lowercase, drop a trailing root dot, and punycode (Caddy treats these spellings as one site)."""
    host = host.strip('"').lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


class _Inspection:
    def __init__(self, domain: str, own_glob: str, allow_wildcard_overlap: bool):
        self.domain, self.own_glob, self.allow_wildcard = domain, own_glob, allow_wildcard_overlap

    def scan(self, text: str) -> list[str]:
        """Check one file for hostname takeover; return path-style imports."""
        imports: list[str] = []
        for _number, tokens, structural, depth in _lines(text):
            if tokens and tokens[0] == "import" and structural[:1] == ["import"]:
                imports.extend(tokens[1:])
            for token in tokens:
                for address in _addresses(token):
                    if address == self.domain:
                        raise RoutingError("hostname is already served by another configuration", "conflict")
                    if address.startswith("*.") and self.domain.endswith(address[1:]) and not self.allow_wildcard:
                        raise RoutingError("hostname overlaps an existing wildcard site", "conflict")
            if depth == 0 and tokens and tokens[0] != "import":
                for token in structural:
                    if "{" in token and token not in {"{", "}"}:
                        raise RoutingError("cannot determine hostnames from a placeholder", "uninspectable")
        return imports

    def own_import_lines(self, text: str) -> list[int]:
        found = []
        for number, tokens, structural, depth in _lines(text):
            if depth == 0 and tokens == ["import", self.own_glob] and structural == tokens:
                found.append(number)
        return found


def _read_file(directory: int, name: str, *, trusted: bool) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise RoutingError("could not open a Caddy file safely", "uninspectable") from None
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES
                or (trusted and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022))):
            raise RoutingError("a Caddy file is not safe to use", "uninspectable")
        return file.read(MAX_CONFIG_BYTES + 1)[:MAX_CONFIG_BYTES]


def _read_path(path: str) -> str:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise RoutingError("an imported Caddy file cannot be inspected", "uninspectable")
        with open(path, "rb") as file:
            return file.read(MAX_CONFIG_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise RoutingError("an imported Caddy file cannot be inspected", "uninspectable") from None


# --- Transactions ---------------------------------------------------------

class RouteManager:
    """Apply, remove, and reconcile managed routes under the host lock."""

    def __init__(self, root: Path, lock: HostLock, commands: CaddyCommands, *,
                 allow_wildcard_overlap: bool = False):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise RoutingError("Caddy root must be absolute", "precondition")
        self.lock, self.commands = lock, commands
        self.allow_wildcard_overlap = allow_wildcard_overlap
        self.own_glob = f"{self.root}/ship/*.caddy"

    # public operations
    def apply(self, registration: dict) -> RouteResult:
        """Install the app's route; skip all work only if the file is already identical."""
        return self._transition(registration, force=False)

    def remove(self, application_id: str) -> RouteResult:
        """Remove the app's route (idempotent when the file is already absent)."""
        return self._transition(application_id, force=False)

    def reconcile(self, application_id: str, registration: dict | None) -> RouteResult:
        """Drive disk and active Caddy to the desired state, always validating and reloading."""
        return self._transition(registration if registration is not None else application_id, force=True,
                                application_id=application_id)

    def inspect_route(self, registration: dict) -> str:
        """Read-only: "absent", "identical", or "different" for the app's route file. Needs no lock."""
        registration = validate_registration(registration)
        root, ship = self._open()
        try:
            current = _read_file(ship, f"{registration['application_id']}.caddy", trusted=True)
        finally:
            os.close(root)
            os.close(ship)
        if current is None:
            return "absent"
        return "identical" if current == render_route(registration["api_domain"], registration["host_port"]) else "different"

    def ensure_import(self) -> RouteResult:
        """Append the managed import to the main Caddyfile once, validated and restorable."""
        if not self.lock.held:
            raise RoutingError("host lock is required", "precondition")
        root, ship = self._open()
        try:
            prior, token = ".prior-Caddyfile", secrets.token_hex(8)
            if os.path.lexists(self.root / prior):
                raise RoutingError("an interrupted Caddyfile change requires manual inspection", "interrupted",
                                   intervention_required=True)
            main = _read_file(root, "Caddyfile", trusted=True)
            if main is None:
                raise RoutingError("the main Caddyfile is missing", "precondition")
            text = main.decode("utf-8", "replace")
            found = _Inspection("", self.own_glob, False).own_import_lines(text)
            if len(found) > 1:
                raise RoutingError("the main Caddyfile imports the managed routes more than once", "precondition")
            if found:
                return RouteResult(changed=False)
            candidate = (text + ("" if not text or text.endswith("\n") else "\n") + f"import {self.own_glob}\n").encode()
            name = _VALIDATE_PREFIX + token
            self._create(root, name, candidate)
            try:
                self.commands.validate(self.root / name)
            except RoutingError as error:
                raise RoutingError("Caddy rejected the Caddyfile with the managed import", "validation",
                                   detail=error.detail) from None
            finally:
                self._unlink(root, name)
            temp = _TEMP_PREFIX + "Caddyfile-" + token
            self._create(root, temp, candidate)
            os.link("Caddyfile", prior, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
            self._publish(root, temp, "Caddyfile")
            try:
                self.commands.reload()
            except RoutingError as error:
                self._restore(root, "Caddyfile", prior, True, error)
                raise RoutingError("Caddy rejected the managed import; the Caddyfile was restored", "reload",
                                   detail=error.detail) from None
            self._unlink(root, prior)
            return RouteResult(changed=True)
        finally:
            os.close(root)
            os.close(ship)

    # implementation
    def _open(self) -> tuple[int, int]:
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise RoutingError("Caddy directory is unavailable", "precondition") from None
        try:
            ship = os.open("ship", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError:
            os.close(root)
            raise RoutingError("Caddy directory is not bootstrapped", "precondition") from None
        try:
            _directory(root)
            _directory(ship)
        except ValueError:
            os.close(root)
            os.close(ship)
            raise RoutingError("Caddy directories are not trusted", "precondition") from None
        return root, ship

    def _transition(self, subject: dict | str, *, force: bool, application_id: str | None = None) -> RouteResult:
        if not self.lock.held:
            raise RoutingError("host lock is required", "precondition")
        if isinstance(subject, dict):
            registration = validate_registration(subject)
            app = registration["application_id"]
            candidate: bytes | None = render_route(registration["api_domain"], registration["host_port"])
            domain: str | None = registration["api_domain"]
            if application_id is not None and application_id != app:
                raise RoutingError("registration does not match the application", "precondition")
        else:
            app, candidate, domain = validate_app_name(subject), None, None
        root, ship = self._open()
        try:
            return self._run(root, ship, app, domain, candidate, force)
        finally:
            os.close(root)
            os.close(ship)

    def _run(self, root: int, ship: int, app: str, domain: str | None, candidate: bytes | None,
             force: bool) -> RouteResult:
        live, prior = f"{app}.caddy", f"{_PRIOR_PREFIX}{app}"
        names = os.listdir(ship)
        for name in names:
            if name.startswith(_TEMP_PREFIX):
                self._unlink(ship, name)  # never read by Caddy; safe to discard
        interrupted = prior in names
        if interrupted and not force:
            raise RoutingError("an interrupted route change requires reconciliation", "interrupted")

        main = _read_file(root, "Caddyfile", trusted=True)
        if main is None:
            raise RoutingError("the main Caddyfile is missing", "precondition")
        main_text = main.decode("utf-8", "replace")
        inspection = _Inspection(domain or "", self.own_glob, self.allow_wildcard_overlap)
        import_lines = inspection.own_import_lines(main_text)
        if len(import_lines) != 1:
            raise RoutingError("the main Caddyfile must import the managed routes exactly once", "precondition")

        # Caddy's "*" glob matches dot-named files, so a stray ".x.caddy" is live configuration too.
        others = sorted(name for name in names if name.endswith(".caddy") and name != live)
        other_texts = {}
        for name in others:
            data = _read_file(ship, name, trusted=True)
            if data is None:
                raise RoutingError("a managed route disappeared during inspection", "uninspectable")
            other_texts[name] = data.decode("utf-8", "replace")
        if domain is not None:
            self._scan_hostnames(inspection, main_text, other_texts)

        current = _read_file(ship, live, trusted=True)
        if not force and current == candidate:
            return RouteResult(changed=False)

        temp = None
        if candidate is not None:
            temp = _TEMP_PREFIX + secrets.token_hex(8)
            self._create(ship, temp, candidate)
        try:
            self._validate(root, main_text, import_lines[0], [f"{self.root}/ship/{name}" for name in others]
                           + ([f"{self.root}/ship/{temp}"] if temp else []))
        except RoutingError:
            if temp:
                self._unlink(ship, temp)
            raise

        changed = current != candidate
        if changed:
            if current is not None:
                self._remove_if_present(ship, prior)
                os.link(live, prior, src_dir_fd=ship, dst_dir_fd=ship, follow_symlinks=False)
            self._publish(ship, temp, live)
        elif temp:
            self._unlink(ship, temp)

        try:
            self.commands.reload()
        except RoutingError as reload_error:
            if not changed:
                raise RoutingError("Caddy could not reload; the active configuration may be stale", "reload",
                                   intervention_required=True, detail=reload_error.detail) from None
            self._restore(ship, live, prior, current is not None, reload_error)
            raise RoutingError("Caddy rejected the new route; the previous route was restored", "reload",
                               detail=reload_error.detail) from None
        self._remove_if_present(ship, prior)
        return RouteResult(changed=changed)

    def _scan_hostnames(self, inspection: _Inspection, main_text: str, others: dict[str, str]) -> None:
        for text in others.values():
            inspection.scan(text)
        seen: set[str] = set()
        queue = [(main_text, str(self.root), 0)]
        while queue:
            text, base, depth = queue.pop()
            for argument in inspection.scan(text):
                if "/" not in argument and "*" not in argument and "." not in argument:
                    continue  # snippet name
                pattern = os.path.normpath(argument if os.path.isabs(argument) else os.path.join(base, argument))
                if pattern == self.own_glob:
                    continue
                if depth + 1 > MAX_IMPORT_DEPTH:
                    raise RoutingError("Caddy imports are nested too deeply to inspect", "uninspectable")
                for path in sorted(glob.glob(pattern)):
                    if path in seen:
                        continue
                    seen.add(path)
                    if len(seen) > MAX_IMPORTED_FILES:
                        raise RoutingError("Caddy imports too many files to inspect", "uninspectable")
                    queue.append((_read_path(path), os.path.dirname(path), depth + 1))

    def _validate(self, root: int, main_text: str, import_line: int, imports: list[str]) -> None:
        lines = main_text.splitlines()
        lines[import_line:import_line + 1] = [f"import {path}" for path in imports]
        name = _VALIDATE_PREFIX + secrets.token_hex(8)
        self._create(root, name, ("\n".join(lines) + "\n").encode())
        try:
            try:
                self.commands.validate(self.root / name)
            except RoutingError as error:
                baseline_ok = True
                try:
                    self.commands.validate(self.root / "Caddyfile")
                except RoutingError:
                    baseline_ok = False
                if not baseline_ok:
                    raise RoutingError("the existing Caddy configuration is already invalid", "validation",
                                       detail=error.detail) from None
                raise RoutingError("Caddy rejected the candidate route", "validation", detail=error.detail) from None
        finally:
            self._unlink(root, name)

    def _restore(self, ship: int, live: str, prior: str, had_prior: bool, cause: RoutingError) -> None:
        try:
            if had_prior:
                os.replace(prior, live, src_dir_fd=ship, dst_dir_fd=ship)
            else:
                os.unlink(live, dir_fd=ship)
            os.fsync(ship)
        except OSError:
            raise RoutingError("the previous route could not be restored", "reload", intervention_required=True,
                               detail=cause.detail) from None
        try:
            self.commands.reload()
        except RoutingError:
            raise RoutingError("Caddy could not reload after restoring the previous route", "reload",
                               intervention_required=True, detail=cause.detail) from None

    @staticmethod
    def _create(directory: int, name: str, data: bytes) -> None:
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.fsync(directory)
        except OSError:
            raise RoutingError("could not write a Caddy file", "precondition") from None

    @staticmethod
    def _publish(ship: int, temp: str | None, live: str) -> None:
        try:
            if temp is None:
                os.unlink(live, dir_fd=ship)
            else:
                os.replace(temp, live, src_dir_fd=ship, dst_dir_fd=ship)
            os.fsync(ship)
        except OSError:
            raise RoutingError("could not publish the route; reconciliation is required", "interrupted",
                               intervention_required=True) from None

    @staticmethod
    def _unlink(directory: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            raise RoutingError("could not remove a temporary Caddy file", "interrupted") from None

    _remove_if_present = _unlink
