# Shiplord

Shiplord ships tested releases of an application to your own Linux server over SSH. A coding agent or a
developer creates an application from the pinned template, provisions it once, and then deploys already-built
artifacts with one command, from a terminal or from GitHub Actions. Nothing is built on the server, no
deployment daemon runs, and every release is verified, journaled, and reversible.

The design and acceptance criteria are in [ship-cli-plan.md](ship-cli-plan.md). The executable is `shiplord`
and the application manifest is `ship.toml`.

## Status

Built and verified (see [docs/evidence.md](docs/evidence.md) for what ran, where, and what did not):

- Application creation from the pinned template, manifest validation, server registration.
- Server bootstrap, application provisioning, SSH keys, runtime environment, port allocation.
- Docker and native-binary deployment, readiness and routing checks, rollback, recovery, status, logs, plan.
- Caddy route transactions with validate-before-publish and restore on failure.

Also run on a real Hetzner server (Ubuntu, x86-64, behind Cloudflare): native deploys, rollback, a failed release
restored automatically, Let's Encrypt HTTPS, and a release deployed by GitHub Actions through Tailscale OIDC. Not
verified: the plan's controlled install on *your* production server (§12E). See [docs/evidence.md](docs/evidence.md).

## Install the CLI

Python 3.11 or later and OpenSSH are required on the machine you work from.

```sh
python -m pip install .
shiplord --help
```

## How it fits together

```
your machine ──ssh──▶ ship-<app>@server ──forced command──▶ dispatcher ──sudo──▶ helper (root, one app)
                                                                                    │ starts a detached job
      administrator ──ssh + sudo──▶ shiplord-admin (bootstrap, provision, keys, env)   ▼
                                                                    worker: journal → runtime → ready → route → commit
```

- **Administrator operations** (`server bootstrap`, `provision`, `env`) use your own SSH login and your own sudo
  (you are asked for the sudo password if one is needed; it is sent on stdin, never in a command line).
- **Application operations** (`deploy`, `status`, `logs`, `rollback`, `recover`, `plan`) use a per-application
  account whose key can run exactly one forced command. The root helper binds every request to that account's
  registration, so an application's deployment account can operate only that application. (The running
  application itself is only confined as described under *Security model*.)
- A deployment is a **job** that outlives your SSH session. A transient systemd unit runs it; you can disconnect
  and read the result later.

## Quick start

```sh
# once per machine: register a server you can already SSH into with a trusted host key
shiplord server add production --ssh appuser@server.example.com

# once per server: installs the helper, wires Caddy and sshd (re-running changes nothing)
shiplord server bootstrap production

# once per application
shiplord init orders --template google-adk
cd orders
shiplord configure --server production --api-domain orders-api.example.com \
  --frontend https://orders.example.com           # or --no-frontend
shiplord provision                                   # registers your SSH key for this application
shiplord env set --from-file .env.production         # runtime secrets; values are never printed
shiplord registry login --username bot --token-file t   # only for a private image: a read-only pull login

# every release
shiplord plan --image ghcr.io/owner/orders@sha256:<digest> --commit <sha>   # read-only
shiplord deploy --image ghcr.io/owner/orders@sha256:<digest> --commit <sha>
shiplord status
shiplord logs --tail 100
shiplord rollback
shiplord recover
```

Create the DNS record for `--api-domain` at your DNS provider; Shiplord checks DNS and reports it but never
changes it.

For a native Linux binary, configure with `--runtime native` and deploy with
`shiplord deploy --binary ./dist/orders --commit <sha>`. Build it for the server's architecture (a Linux ELF;
x86-64 and arm64 are supported), for example in CI. The binary is uploaded over the same SSH connection,
verified by size, SHA-256 and ELF header on the server, and run as an unprivileged account under a confined
systemd unit.

## Deploying from GitHub Actions

```sh
shiplord github setup --repo owner/orders --tooling-ref <commit of Shiplord pushed to GitHub>
git add .github/workflows/shiplord-release.yml ship.toml && git commit   # merge to the deployment branch
shiplord github check-ci            # joins Tailscale and logs in with the CI key; builds and deploys nothing
shiplord github status --check-server
shiplord github rotate-key
```

`github setup` uses your authenticated `gh`. It creates a dedicated Ed25519 CI key, registers it on the server,
**proves it authenticates over SSH against the server's pinned host keys** (read over your already-trusted
connection, never from `ssh-keyscan`), and only then stores it as the `SHIPLORD_SSH_PRIVATE_KEY` secret (through
stdin) and sets the `SHIPLORD_*` repository variables. It then writes `.github/workflows/shiplord-release.yml`:
the template's quality checks, an image build, and `shiplord deploy` by digest from a runner joined to your tailnet.
It is resumable, keeps the private key only until GitHub has it, and never uploads your personal key.

Two things stay with you because they need authority Shiplord does not have: the **Tailscale trust credential** and
**DNS**. For Tailscale, `github setup` guides you instead: it reads the OIDC subject GitHub will actually present (repositories
with immutable subjects use `repo:Owner@<id>/name@<id>`, not the plain names), prints the policy line and the
credential settings with every value filled in, asks for the client ID and audience (not secrets), checks they belong
together, and saves them so the next application's setup does not ask again. Use the owner-wide subject it suggests,
`repo:Owner@<id>/*:ref:refs/heads/main`, to do this once for every repository. `--tailscale-client-id` and
`--tailscale-audience` skip the prompt. `github check-ci` then proves the whole path with a check-only run. The generated workflow pins every action to a commit, never cancels a running
production deploy, and gives `id-token: write` only to the jobs that need it. `rotate-key` adds and proves a new key,
updates GitHub, and only then revokes the old one.

CI runs `shiplord` in pinned mode: `SHIPLORD_SSH_TARGET`, `SHIPLORD_SSH_IDENTITY`, `SHIPLORD_SSH_KNOWN_HOSTS_FILE`
(and optionally `SHIPLORD_SSH_PORT`) replace the server alias, and SSH ignores `~/.ssh/config` entirely.

## Commands

| Command | Purpose |
| --- | --- |
| `server add <alias> --ssh user@host` | Register a trusted SSH login. Never accepts or updates host keys. |
| `server bootstrap <alias>` | Install or update the helper on the server. Idempotent. |
| `init`, `configure` | Create a project from the pinned template; create `ship.toml`. |
| `provision` | Create the application's account, state, port, and key access on the server. |
| `env set --from-file F` / `env keys` / `env clear` | Manage runtime environment. `set` refuses an empty file; `keys` lists names only. |
| `plan [--image I \| --binary B] --commit C` | Local preview, or with an artifact, what the server would do. Changes nothing. |
| `deploy --image I \| --binary B --commit C` | Deploy a built artifact. `--request-id` makes retries safe. |
| `status`, `logs --tail N` | Release state, running state, unfinished work; bounded logs. |
| `rollback`, `recover` | Restore the previous release; reconcile an interrupted deployment. |
| `registry login --username U --token-file F \| --token-stdin` / `logout` / `status` | A per-application read-only pull login for private images. There is no `--token` option. |
| `github setup --repo R` / `check-ci` / `status [--check-server]` / `rotate-key` | CI key, secret and variables, guided Tailscale setup, release workflow; a check-only CI run. |
| `doctor --local \| --server <alias> [--deep] \| (in an app directory)` | Prerequisites; helper probe (`--deep`: Caddy, sshd, Docker, disk, each application); access, helper, DNS. |

Every command that reports a result accepts `--json`. No output contains private keys, secret values, or complete
environments.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Succeeded. A deployment is verified ready. |
| 1 | The operation failed; read the result. Also: stale release, recovery required. |
| 2 | Invalid arguments, configuration, or request. |
| 3 | A prerequisite is missing: SSH trust, credentials, permissions, sudo. |
| 4 | Incompatible CLI and helper protocol versions. |
| 5 | The deployment failed and the previous release was restored. Still a failure. |
| 6 | Recovery failed or manual intervention is required; an interrupted job is also reported as 6. |

## What a deployment does

1. The helper checks the request against the application's registration (hostname, runtime, image repository)
   and refuses anything that would change them.
2. **Prepare, without touching the running app:** pull the image by digest (platform and revision label
   verified) or verify and install the uploaded binary. A failure here changes nothing.
3. **Journal** the transaction, replace the runtime (a brief restart window), poll the readiness path.
4. Apply the Caddy route only if it changed, verify the active configuration routes the hostname, then commit.
5. On any failure, restore the last committed release, verify it, and only then clear the journal. The command
   still exits nonzero. A first release that fails leaves no runtime and no route.
6. A candidate that failed leaves nothing behind; the image or binary of a release that leaves the
   two-release window is removed.

Releases are ordered by the commit's committer timestamp (`--sequence` overrides). An honest client's queued older
release cannot replace a newer one (the server refuses it); the check relies on the sequence the caller supplies, and
`--sequence`/`allow_older` exist, so it protects against mistakes, not against a malicious account. Use `rollback` to go
back deliberately. Database and persistent data are never
rolled back.

If a worker is killed mid-deployment the journal remains, further deployments are refused with
`recovery_required`, and `shiplord recover` restores the last committed release.

## Security model

- Per-application deployment accounts; authorized keys are root-owned and carry `command=...,restrict`.
- The dispatcher never evaluates the client's command line; sudo permits one fixed helper with no arguments.
- Application ports bind to loopback only; Caddy is the only public entry point.
- Runtime environment files are root-only; secrets never appear in argv, logs, results, or generated files.
- Host keys are pinned (`StrictHostKeyChecking=yes`); a changed key fails closed.
- Containers run with a memory limit, bounded logs, `no-new-privileges` and all capabilities dropped; native
  services run as a separate unprivileged account with `ProtectSystem=strict` and no capabilities.

### Limits of the confinement

- A deployment account may request at most the application's registered memory ceiling (default 4096 MiB;
  `provision --max-memory-mb`), and only the current and previous releases are kept (`retain_successful_releases` must be 2).
- Native services may bind only their own port (`SocketBindAllow`), but **can still connect to other loopback services**
  on the host, including Caddy's admin API on `127.0.0.1:2019`. Docker applications share Docker's default bridge.
  Do not run an untrusted application on a host that holds anything else you care about; for stronger isolation move
  Caddy's admin endpoint to a unix socket.
- A deployment account cannot be used to run arbitrary code as root, but anyone holding its key can deploy whatever
  that application's registration allows.

## Removing an application

`shiplord deprovision --yes` removes the account, keys, registration and state of an application that has no release
deployed. `--retire` first removes the route, stops the runtime and discards its releases. Application data (the data
directory, runtime account and Docker volume) is **never** deleted; remove it by hand when you are sure.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v          # unit tests; real-host suites are skipped
tests/linux/run.sh all                            # real sshd, sudo, Caddy, Docker, systemd in containers
```

`tests/linux/run.sh <unit|caddy|host|docker|e2e|systemd>` runs one suite; see the script header. These suites
change `/etc` and system accounts, so they refuse to run outside a disposable container or CI runner.

The server-side helper code is stdlib-only and is installed from the same source as the CLI. See
[docs/protocol.md](docs/protocol.md) for the wire protocol and [docs/operations.md](docs/operations.md) for the
host layout and troubleshooting.
