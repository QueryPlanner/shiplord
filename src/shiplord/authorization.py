"""Application binding for a root helper invoked under a restricted sudo policy."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
from pathlib import Path

from .protocol import _invalid_constant, _unique_keys
from .registry import validate_registration

REGISTRATION_ROOT = Path("/etc/shiplord/apps")
_APP = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


class AuthorizationError(ValueError):
    """The application principal could not be established."""


def _trusted_directory(path: Path) -> int:
    """Open a root-owned directory without following any path symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in ("", *path.parts[1:]):
            if component:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise AuthorizationError("untrusted registration directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def authorize_application(application_id: object) -> dict:
    """Require consistent sudo identity and a root-owned app registration.

    Returns the validated registration, which is the source of truth for the
    application's runtime, hostname, image repository, and host port.

    The effective sudo policy is the security boundary. Root can forge sudo
    environment variables, so this check does not authenticate root itself.
    """
    if (os.geteuid() != 0 or not isinstance(application_id, str)
            or len(application_id) > 48 or not _APP.fullmatch(application_id)):
        raise AuthorizationError("application access denied")
    uid_text = os.environ.get("SUDO_UID", "")
    user = os.environ.get("SUDO_USER", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", uid_text) or not user:
        raise AuthorizationError("application access denied")
    uid = int(uid_text)
    try:
        if pwd.getpwuid(uid).pw_name != user:
            raise AuthorizationError("application access denied")
        directory = _trusted_directory(REGISTRATION_ROOT)
        try:
            descriptor = os.open(application_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as file:
                info = os.fstat(file.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                        or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > 4096):
                    raise AuthorizationError("application access denied")
                data = file.read(4097)
        finally:
            os.close(directory)
        if len(data) > 4096:
            raise AuthorizationError("application access denied")
        registration = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
        registration = validate_registration(registration, application_id)
        if registration["deployment_uid"] != uid or registration["deployment_user"] != user:
            raise AuthorizationError("application access denied")
        return registration
    except (OSError, KeyError, UnicodeDecodeError, ValueError):
        raise AuthorizationError("application access denied") from None
