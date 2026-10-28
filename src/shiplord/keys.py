"""Strict parsing of SSH public keys and a root-owned per-application key store.

Only the key type and blob are ever written to an authorized-keys file, always
behind a forced command, so neither client-supplied options nor comments can
reach sshd. The store refuses to rewrite lines it did not generate.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from .layout import DISPATCH_PATH
from .manifest import validate_app_name

MAX_KEY_LINE = 8192
MAX_KEYS = 16
MIN_RSA_BITS = 3072
_TYPES = {"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa"}
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")
_LINE = re.compile(r'command="(?P<command>[^"\\]+)",restrict,no-pty (?P<type>[a-z0-9-]+) (?P<blob>[A-Za-z0-9+/]+={0,2}) '
                   r"shiplord:(?P<label>[a-z0-9][a-z0-9._-]{0,31})\Z")


class KeyError_(ValueError):
    """A public key or key store is invalid."""


@dataclass(frozen=True)
class PublicKey:
    type: str
    blob: str  # base64, as it appears in authorized_keys

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(base64.b64decode(self.blob)).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _strings(blob: bytes) -> list[bytes]:
    parts, offset = [], 0
    while offset < len(blob):
        if offset + 4 > len(blob):
            raise KeyError_("malformed public key")
        (length,) = struct.unpack(">I", blob[offset:offset + 4])
        offset += 4
        if length > len(blob) - offset:
            raise KeyError_("malformed public key")
        parts.append(blob[offset:offset + length])
        offset += length
    return parts


def validate_label(label: object) -> str:
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise KeyError_("key label must be lowercase letters, digits, dots, dashes, or underscores")
    return label


def parse_public_key(line: object) -> PublicKey:
    """Parse one OpenSSH public key line; options and comments are rejected or dropped."""
    if not isinstance(line, str) or len(line) > MAX_KEY_LINE or "\n" in line.strip("\n") or "\r" in line:
        raise KeyError_("invalid public key")
    fields = line.strip().split()
    if len(fields) < 2 or fields[0] not in _TYPES:
        raise KeyError_("unsupported public key type or options present")
    key_type, blob_text = fields[0], fields[1]
    if not _BASE64.fullmatch(blob_text):
        raise KeyError_("malformed public key")
    try:
        blob = base64.b64decode(blob_text, validate=True)
    except (binascii.Error, ValueError):
        raise KeyError_("malformed public key") from None
    parts = _strings(blob)
    if not parts or parts[0].decode("ascii", "replace") != key_type:
        raise KeyError_("public key type does not match its contents")
    if key_type == "ssh-ed25519":
        if len(parts) != 2 or len(parts[1]) != 32:
            raise KeyError_("malformed ed25519 public key")
    elif key_type == "ssh-rsa":
        if len(parts) != 3 or not parts[2] or int.from_bytes(parts[2], "big").bit_length() < MIN_RSA_BITS:
            raise KeyError_(f"RSA keys must be at least {MIN_RSA_BITS} bits")
    elif len(parts) != 3:
        raise KeyError_("malformed ECDSA public key")
    return PublicKey(key_type, base64.b64encode(blob).decode())


class KeyStore:
    """The authorized-keys file of one deployment account, owned by root and not writable by it."""

    def __init__(self, directory: Path, application_id: str, account: str):
        self.application_id = validate_app_name(application_id)
        self.account = account
        self.path = Path(directory) / account
        self.command = f"{DISPATCH_PATH} {self.application_id}"

    def _read(self) -> list[tuple[str, PublicKey]]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        except OSError:
            raise KeyError_("could not open the key file safely") from None
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
                    or info.st_size > MAX_KEY_LINE * (MAX_KEYS + 1)):
                raise KeyError_("the key file is not safe to use")
            text = file.read().decode("utf-8", "replace")
        entries = []
        for line in text.splitlines():
            match = _LINE.fullmatch(line)
            if not match or match["command"] != self.command:
                raise KeyError_("the key file contains a line Shiplord did not write")
            entries.append((match["label"], parse_public_key(f"{match['type']} {match['blob']}")))
        return entries

    def list(self) -> list[tuple[str, str]]:
        return [(label, key.fingerprint) for label, key in self._read()]

    def add(self, label: str, key: PublicKey) -> bool:
        """Add a key; return False if the identical entry exists. Conflicts are refused."""
        label = validate_label(label)
        entries = self._read()
        for existing_label, existing in entries:
            if existing_label == label:
                if existing == key:
                    return False
                raise KeyError_("the label is already used by a different key")
            if existing == key:
                raise KeyError_("the key is already registered under another label")
        if len(entries) >= MAX_KEYS:
            raise KeyError_("too many keys are registered for this application")
        self._write([*entries, (label, key)])
        return True

    def remove(self, label: str) -> bool:
        label = validate_label(label)
        entries = self._read()
        remaining = [entry for entry in entries if entry[0] != label]
        if len(remaining) == len(entries):
            return False
        self._write(remaining)
        return True

    def delete(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _write(self, entries: list[tuple[str, PublicKey]]) -> None:
        text = "".join(f'command="{self.command}",restrict,no-pty {key.type} {key.blob} shiplord:{label}\n'
                       for label, key in entries)
        temporary = self.path.with_name(f".{self.path.name}.tmp-{secrets.token_hex(8)}")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "w") as file:
                    file.write(text)
                    file.flush()
                    os.fchmod(file.fileno(), 0o644)  # sshd reads it as the deployment user
                    if os.geteuid() == 0:
                        os.fchown(file.fileno(), 0, 0)
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            raise KeyError_("could not write the key file") from None
