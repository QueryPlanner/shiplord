"""Generate a project from a pinned, public ADK template snapshot."""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from .manifest import ManifestError, validate_app_name

TEMPLATE_COMMIT = "f6c277605742187312a100de8d3ad6c8ac4eea0f"
TEMPLATE_REPOSITORY = "QueryPlanner/google-adk-on-bare-metal"
TEMPLATE_URL = f"https://codeload.github.com/{TEMPLATE_REPOSITORY}/tar.gz/{TEMPLATE_COMMIT}"
MAX_DOWNLOAD = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBERS = 5000


class TemplateError(ValueError):
    """A template could not be safely initialized."""


def _download() -> bytes:
    try:
        request = urllib.request.Request(TEMPLATE_URL, headers={"User-Agent": "shiplord"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    except (OSError, urllib.error.URLError):
        raise TemplateError("could not download the pinned template") from None
    if len(data) > MAX_DOWNLOAD:
        raise TemplateError("template download exceeds the size limit")
    return data


def _extract(data: bytes, destination: Path) -> None:
    expected_root = f"google-adk-on-bare-metal-{TEMPLATE_COMMIT}"
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise TemplateError("template contains too many entries")
                name = member.name.rstrip("/")
                parts = PurePosixPath(name).parts
                if (not parts or parts[0] != expected_root or name.startswith("/")
                        or "\\" in name or any(part.lower() in {"", ".", "..", ".git"} for part in name.split("/"))
                        or name in seen):
                    raise TemplateError("template contains an unsafe or duplicate path")
                seen.add(name)
                if not (member.isfile() or member.isdir()):
                    raise TemplateError("template contains an unsupported file type")
                relative = parts[1:]
                if not relative:
                    if not member.isdir():
                        raise TemplateError("template root is not a directory")
                    continue
                output = destination.joinpath(*relative)
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    total += member.size
                    if member.size < 0 or total > MAX_EXPANDED:
                        raise TemplateError("expanded template exceeds the size limit")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    with source, output.open("xb") as file:
                        shutil.copyfileobj(source, file, length=65536)
                    output.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError, EOFError):
        raise TemplateError("template archive could not be extracted safely") from None
    if expected_root not in seen:
        raise TemplateError("template archive has no root directory")


def _replace_once(path: Path, old: str, new: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise TemplateError("pinned template content does not match expectations")
        path.write_text(text.replace(old, new), encoding="utf-8")
    except (OSError, UnicodeError):
        raise TemplateError("pinned template content could not be adapted") from None


SHIPLORD_GUIDE = """# Releasing with Shiplord

This project deploys to a server with `shiplord`. The server never builds anything; it runs the image CI built.
Existing template files are untouched: Shiplord starts the container itself and allocates the host port, so the
template's own `compose.yaml` is not used on the server.

## One-time setup (a person with server access)

```sh
shiplord server add production --ssh <admin>@<server>     # a server you can already SSH into
shiplord server bootstrap production                      # once per server
shiplord configure --server production --api-domain <api hostname> --frontend <origin>   # or --no-frontend
shiplord provision
shiplord env set --from-file <file with DATABASE_URL, API keys, ...>
shiplord registry login --username <user> --token-file <read-only token>   # only for a private image
shiplord github setup --repo <owner>/<repository> --tooling-ref <shiplord commit>
```

Create the DNS record for the API hostname yourself. Finish the Tailscale federation described in the output of
`shiplord github setup`, then push to `main`.

## Releasing

Merging to `main` runs `.github/workflows/shiplord-release.yml`: the template's quality checks, an image build, and
`shiplord deploy` by digest. To release by hand: `shiplord deploy --image <registry/name@sha256:digest> --commit <sha>`.

## Reading results (exit codes)

| Exit | Meaning | Do |
| --- | --- | --- |
| 0 | Deployed and verified ready | Report the commit and the API hostname from `ship.toml` |
| 1 | Failed, stale, or recovery required | Read the message; `shiplord recover` if told to |
| 2 | Invalid input | Fix it; do not retry unchanged |
| 3 | Missing prerequisite (SSH, sudo, DNS) | `shiplord doctor` |
| 5 | The release failed and the previous one is serving | Report it as a failure; see `shiplord logs` |
| 6 | Needs intervention | `shiplord recover` |

`shiplord status` shows what is deployed. `shiplord rollback` returns to the previous release. Never run Docker,
systemctl, or Caddy on the server by hand, and never put secrets in `ship.toml` or on a command line.
"""


def _adapt(directory: Path, name: str) -> None:
    _replace_once(directory / "pyproject.toml", 'name = "google-adk-on-bare-metal"', f'name = "{name}"')
    _replace_once(directory / "uv.lock", 'name = "google-adk-on-bare-metal"', f'name = "{name}"')
    _replace_once(directory / ".env.example", "AGENT_NAME=local-agent", f"AGENT_NAME={name}")
    _replace_once(directory / "tests/test_dependabot.py", 'package["name"] == "google-adk-on-bare-metal"', 'package["name"] == project["project"]["name"]')
    _replace_once(directory / "tests/test_deployment_workflow.py", 'package["name"] == "google-adk-on-bare-metal"', 'package["name"] == project["project"]["name"]')
    (directory / "SHIPLORD.md").write_text(SHIPLORD_GUIDE, encoding="utf-8")  # a new file: no template file is edited
    (directory / ".shiplord-template.json").write_text(json.dumps({
        "repository": TEMPLATE_REPOSITORY, "commit": TEMPLATE_COMMIT, "application": name,
    }, indent=2) + "\n", encoding="utf-8")


def _publish(source: Path, parent: Path, name: str) -> None:
    """Create each destination exclusively, without following directory symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    target = parent / name
    try:
        try:
            target.mkdir()
        except FileExistsError:
            pass
        root = os.open(target, flags)
        try:
            if os.listdir(root):
                raise TemplateError("destination is not empty; no files were replaced")
            for item in sorted(source.rglob("*")):
                parts = item.relative_to(source).parts
                current = os.dup(root)
                try:
                    for component in parts[:-1]:
                        next_fd = os.open(component, flags, dir_fd=current)
                        os.close(current)
                        current = next_fd
                    if item.is_dir():
                        os.mkdir(parts[-1], 0o755, dir_fd=current)
                    else:
                        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                             item.stat().st_mode & 0o777, dir_fd=current)
                        with os.fdopen(descriptor, "wb") as dst, item.open("rb") as src:
                            shutil.copyfileobj(src, dst)
                finally:
                    os.close(current)
        finally:
            os.close(root)
    except OSError:
        raise TemplateError("could not publish the template; the destination may contain partial files") from None


def create_project(parent: Path, name: str, template: str) -> Path:
    name = validate_app_name(name)
    if template != "google-adk":
        raise ManifestError("template must be google-adk")
    target = parent / name
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise TemplateError("destination is not an empty directory; no files were replaced")
    data = _download()
    with tempfile.TemporaryDirectory(prefix="shiplord-template-") as temporary:
        stage = Path(temporary)
        _extract(data, stage)
        _adapt(stage, name)
        _publish(stage, parent, name)
    return target
