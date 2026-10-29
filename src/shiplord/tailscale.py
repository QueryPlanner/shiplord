"""Guided Tailscale workload-identity setup for GitHub Actions.

Shiplord never holds a Tailscale credential. It works out the exact values the Tailscale admin console needs (above
all the OIDC subject GitHub will present, which differs for repositories using immutable subjects), checks what the
user pastes back, and remembers the two non-secret identifiers so the next application can reuse them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .servers import _config_home

TRUST_CREDENTIALS_URL = "https://login.tailscale.com/admin/settings/trust-credentials"
POLICY_URL = "https://login.tailscale.com/admin/acls"
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
DEFAULT_TAG = "tag:ci"
_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,127}\Z")
_PREFIX = re.compile(r"repo:([^/\s:]+)/([^/\s:]+)\Z")


class TailscaleError(ValueError):
    """A Tailscale setting is unusable."""


def subjects(prefix: str, branch: str) -> dict[str, str]:
    """The OIDC subjects for pushes to ``branch``: this repository only, and every repository of the same owner.

    ``prefix`` is GitHub's subject prefix for the repository, ``repo:<owner>/<repo>``, where immutable subjects carry
    numeric IDs (``repo:Owner@123/name@456``). The owner part, IDs included, never changes, so the wildcard form is
    safe to reuse for later repositories.
    """
    match = _PREFIX.fullmatch(prefix)
    if not match:
        raise TailscaleError(f"unexpected OIDC subject prefix from GitHub: {prefix!r}")
    return {"repository": f"{prefix}:ref:refs/heads/{branch}",
            "all_repositories": f"repo:{match.group(1)}/*:ref:refs/heads/{branch}"}


def guidance(prefix: str, branch: str, *, custom_claims: list[str] | None = None, tag: str = DEFAULT_TAG) -> dict:
    """Step-by-step instructions with every value filled in."""
    values = subjects(prefix, branch)
    steps = [
        f"In {POLICY_URL} add the tag to tagOwners (once per tailnet): \"{tag}\": [\"autogroup:admin\"]. "
        f"If your grants are not allow-all, also allow {tag} to reach the server's SSH port.",
        f"In {TRUST_CREDENTIALS_URL} choose Credential → OpenID Connect, issuer GitHub ({GITHUB_ISSUER}).",
        f"Subject: {values['all_repositories']}  (every repository of this owner, so you do this once), "
        f"or {values['repository']}  (this repository only).",
        f"Scopes: Auth Keys with write access; tags: {tag}. Generate the credential.",
        "Copy the Client ID and the Audience. Neither is a secret.",
    ]
    result = {"subject_all_repositories": values["all_repositories"], "subject_repository": values["repository"],
              "issuer": GITHUB_ISSUER, "tag": tag, "steps": steps}
    if custom_claims:
        result["warning"] = ("this repository uses a customized OIDC subject template (" + ", ".join(custom_claims) +
                             "); the subjects above assume GitHub's default and may not match")
    return result


def validate(client_id: str, audience: str) -> tuple[dict[str, str], list[str]]:
    """Check pasted values; return the repository variables and any warnings."""
    client_id, audience = client_id.strip(), audience.strip()
    if not _CLIENT_ID.fullmatch(client_id):
        raise TailscaleError("the Tailscale client ID should be the letters, digits and dashes shown in the console")
    if not audience or any(char.isspace() for char in audience) or len(audience) > 512:
        raise TailscaleError("the Tailscale audience must be a single value without spaces")
    warnings = []
    if client_id not in audience:
        warnings.append("the audience does not contain the client ID; Tailscale's default audience is "
                        "api.tailscale.com/<client ID>, so check that both came from the same credential")
    return {"SHIPLORD_TS_CLIENT_ID": client_id, "SHIPLORD_TS_AUDIENCE": audience}, warnings


def _defaults_path(home: Path | None = None) -> Path:
    return (home or _config_home()) / "shiplord" / "tailscale.json"


def load_defaults(home: Path | None = None) -> dict[str, str] | None:
    """The client ID and audience saved by an earlier setup, if any."""
    try:
        data = json.loads(_defaults_path(home).read_text())
        settings, _ = validate(data["client_id"], data["audience"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return settings


def save_defaults(settings: dict[str, str], home: Path | None = None) -> None:
    path = _defaults_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".tailscale.tmp-{os.getpid()}")
    temporary.write_text(json.dumps({"client_id": settings["SHIPLORD_TS_CLIENT_ID"],
                                     "audience": settings["SHIPLORD_TS_AUDIENCE"]}) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def resolve(given: dict[str, str], *, ask=None, guide=None, home: Path | None = None) -> tuple[dict[str, str], dict]:
    """Decide the Tailscale settings: flags first, then the saved defaults, then (if ``ask``) the user.

    ``guide()`` returns the instructions to show before asking; ``ask(instructions)`` returns ``(client_id,
    audience)`` or ``None`` to skip. Settings that came from flags or the prompt are saved for the next application.
    Returns the settings (empty when skipped) and a note of where they came from, with any warnings.
    """
    tags = {key: value for key, value in given.items() if key == "SHIPLORD_TS_TAGS"}
    client_id, audience = given.get("SHIPLORD_TS_CLIENT_ID"), given.get("SHIPLORD_TS_AUDIENCE")
    if client_id or audience:
        if not (client_id and audience):
            raise TailscaleError("pass both --tailscale-client-id and --tailscale-audience")
        settings, warnings = validate(client_id, audience)
        save_defaults(settings, home)
        return {**settings, **tags}, {"source": "options", "warnings": warnings}
    saved = load_defaults(home)
    if saved:
        return {**saved, **tags}, {"source": "saved from an earlier setup", "warnings": []}
    if ask is None or guide is None:
        return tags, {"source": "not configured", "warnings": []}
    answer = ask(guide())
    if not answer:
        return tags, {"source": "skipped", "warnings": []}
    settings, warnings = validate(*answer)
    save_defaults(settings, home)
    return {**settings, **tags}, {"source": "entered", "warnings": warnings}
