# Shiplord

Shiplord is an early-stage CLI for managing application deployments. The design
is in [ship-cli-plan.md](ship-cli-plan.md). The executable is named `shiplord`;
the application manifest remains `ship.toml`.

This first milestone validates a local manifest, previews its configuration,
checks a few local prerequisites, and defines a provisional protocol envelope.
All server, deployment, template generation, and GitHub setup operations report
that they are not implemented. No server helper has been built or installed.

## Install

Python 3.11 or later is required. In a virtual environment, run:

```sh
python -m pip install .
shiplord --help
```

## Local preview

Create `ship.toml` in the application directory. This example contains no secrets:

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
