# Shiplord

Shiplord is an early-stage CLI for managing application deployments. The design
is in [ship-cli-plan.md](ship-cli-plan.md). The executable is named `shiplord`;
the application manifest remains `ship.toml`.

The CLI generates a project from a pinned template, creates and validates a
local manifest, checks a few local prerequisites, and defines a provisional
protocol envelope. Server, deployment, and GitHub setup operations report that
they are not implemented. No server helper has been built or installed.

## Install

Python 3.11 or later is required. In a virtual environment, run:

```sh
python -m pip install .
shiplord --help
```

## Local preview

Generate a project and create a manifest in its directory:

```sh
shiplord init orders --template google-adk
cd orders
shiplord configure --name orders --server production \
  --api-domain orders-api.example.com --frontend https://orders.example.com
```

Repeat `--frontend` for multiple origins, or use `--no-frontend` for an explicit
empty list. The command refuses to replace an existing `ship.toml`. It does not
verify the server alias. `--name` defaults to the current directory name.

Initialization fetches the public template at commit
`f6c277605742187312a100de8d3ad6c8ac4eea0f` over HTTPS from GitHub. It records
the commit in `.shiplord-template.json`. GitHub's archive endpoint and TLS are
the trust source; Shiplord does not independently verify Git objects. The
template's Python package remains named `agent`. GitHub repository customization
and deployment workflow integration are not implemented yet. Initialization
refuses nonempty directories and does not overwrite files. If publishing files
to the target fails, the error reports that the target may contain partial files.

The manifest contains no secrets. A GitHub section can be added once the
repository is known. For example:

```toml
schema_version = 1
name = "orders"
server = "production"
template = "google-adk"
runtime = "docker"

[http]
api_domain = "orders-api.example.com"
frontend_origins = ["https://orders.example.com"]
container_port = 8080
readiness_path = "/ready"

[resources]
memory_mb = 512

[deploy]
readiness_timeout_seconds = 90
retain_successful_releases = 2

[github]
repository = "QueryPlanner/orders"
deployment_branch = "main"
```

Run `shiplord plan --json` to validate and preview this local configuration.
It does not inspect server state or predict the changes a server operation would
make. `shiplord doctor --local --json` checks for `ssh`, `git`, and `gh` on PATH;
it does not check server readiness. Exit code 2 indicates an invalid manifest,
3 indicates a missing local prerequisite, and 1 indicates an unfinished command.

Do not put credentials or application secrets in the manifest. The preview
prints its validated nonsecret fields.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

The protocol envelope is provisional. `decode_request` validates its outer
shape, size, and version only. It does not authorize requests or validate
operation-specific arguments. A helper must implement and test capability
negotiation, authorization, transport, and action validation before accepting
requests or making changes.
