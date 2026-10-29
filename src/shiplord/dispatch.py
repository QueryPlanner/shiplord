"""Forced-command dispatcher for SSH logins.

Invoked as ``shiplord-dispatch`` (an administrator's capabilities probe) or as
``shiplord-dispatch <app>`` from an application-bound ``command=`` option in the
root-owned authorized-keys file. The client's command line is never evaluated;
it must equal the dispatcher path or be empty. Application access is decided by
the privileged helper, which binds the sudo caller to its registration.
"""

from __future__ import annotations

import os
import pwd
import sys
from typing import Mapping

from .layout import DISPATCH_PATH, HOST_PATH, deployment_user
from .manifest import ManifestError, validate_app_name

SUDO = "/usr/bin/sudo"
EXEC_ENVIRONMENT = {"PATH": "/usr/bin:/bin"}


class DispatchError(Exception):
    """The request cannot be dispatched; ``code`` is the process exit status."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def plan_dispatch(argv: list[str], environ: Mapping[str, str], username: str) -> tuple[str, list[str]]:
    """Decide what to run: ("capabilities", []) or ("exec", argv for sudo)."""
    if len(argv) == 1:
        return "capabilities", []
    if len(argv) != 2:
        raise DispatchError("invalid dispatcher arguments", 2)
    try:
        app = validate_app_name(argv[1])
    except ManifestError:
        raise DispatchError("invalid application", 2) from None
    if environ.get("SSH_ORIGINAL_COMMAND", "") not in ("", DISPATCH_PATH):
        raise DispatchError("unsupported command", 2)
    if username != deployment_user(app):
        raise DispatchError("login does not match the application", 3)
    return "exec", [SUDO, "-n", "--", HOST_PATH]


def main() -> None:
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        action, command = plan_dispatch(sys.argv, os.environ, username)
    except (DispatchError, KeyError) as error:
        print(str(error) if isinstance(error, DispatchError) else "unknown account", file=sys.stderr)
        raise SystemExit(error.code if isinstance(error, DispatchError) else 3)
    if action == "capabilities":
        from .host import main as capabilities
        capabilities()
        return
    os.execve(command[0], command, EXEC_ENVIRONMENT)


if __name__ == "__main__":
    main()
