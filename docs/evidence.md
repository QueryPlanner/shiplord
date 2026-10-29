# Evidence for the plan's Definition of Done

This is an audit of `ship-cli-plan.md` §13 against what was actually run. "Verified" names the test or suite that
exercised the behavior. Anything not run is listed as not verified; nothing here is inferred.

State of the tree when this was written: the working tree on `main`, uncommitted. All numbers are from the runs described below.

## How it was verified

| Where | Command | Result |
| --- | --- | --- |
| macOS (arm64), Python 3.14 | `python -m unittest discover -s tests` | 611 run, OK, 60 skipped (real-host suites) |
| Linux container (Ubuntu 24.04), integration as root + full suite as an unprivileged user | `tests/linux/run.sh unit` | 611 run as an unprivileged user, OK (74 skipped); sudo/sshd integration 8/8 as root |
| Real Caddy 2.11.7: route transactions, reload, live HTTPS traffic through Caddy's local CA | `tests/linux/run.sh caddy` | 5/5 passed |
| Real sshd, sudo, useradd, visudo: bootstrap, provisioning, the SSH forced-command path | `tests/linux/run.sh host` | 9/9 passed |
| Real Docker daemon and registry: the deploy engine, rollback, recovery, retention | `tests/linux/run.sh docker` | 1/1 passed (full deploy/rollback/recovery/retention lifecycle) |
| The real `shiplord` CLI against real sshd, sudo, Caddy, Docker; two applications; GitHub setup and CI simulation | `tests/linux/run.sh e2e` | 24/24 passed |
| The native lifecycle on real systemd (PID 1), real transient jobs, journald, `systemctl reload caddy` | `tests/linux/run.sh systemd` | 13/13 passed |
| The generated project, against the template's own 1,200-test suite | `uv run pytest tests` in a project made by `shiplord init` | 1,196 passed; 5 failed, of which 2 also fail on the untouched template on this machine and 3 passed when rerun alone (load flakes) |

The Linux suites run in containers on Docker Desktop (a Linux VM on arm64). GitHub-hosted Ubuntu 24.04 x86-64
runners were **not** used; `.github/workflows/test.yml` defines the corresponding jobs but they have not run.

## Definition of Done (§13)

| Item | Status | Evidence |
| --- | --- | --- |
| CLI installs and runs on the MacBook without server source paths | Verified | Non-editable wheel install into a clean venv: `shiplord --version`, helper packaging from `site-packages`. macOS suite. |
| Server bootstrap through normal administrator SSH/sudo, safely repeatable | Verified on a disposable host, **not on your server** | `test_e2e` 01-02 (second run changes nothing, upload directory removed), `test_host_integration`, systemd 01. Password-sudo path: e2e 14. |
| Protocol and version compatibility documented and tested | Verified | `docs/protocol.md`; `test_protocol`, `test_framing`, `test_helper_actions`, `test_provision` (version 4 path), admin and helper incompatibility tests. |
| Existing Caddy routes and running applications survive integration | Verified | Bootstrap only appends one validated import line (`test_routing`, `test_caddy_integration`); the default Caddyfile site survives in e2e and systemd runs; unrelated route files are byte-identical after every outcome; a second application is undisturbed by the first's failed deploy (e2e 23). |
| A new ADK application can be generated from the pinned template | Verified | `shiplord init` against the real pinned commit; the template's own tests on the result. The overlay adds one file (`SHIPLORD.md`). |
| Provisioning sets up a unique port, route, runtime, data, deployment account, key access | Verified | `test_provision`, `test_registry`, e2e 03 and 23 (distinct port/account/volume/route), systemd 01 (runtime account, private data). The route is created at first deploy, by design. |
| Frontend origins reach ALLOW_ORIGINS correctly | Partly verified | Serialization verified in a real container and a real systemd service (`/env/ALLOW_ORIGINS`). The real ADK server's CORS behavior (plan §12B scenario 6) was **not** exercised. |
| Docker deployment uses a tested immutable image built outside the VM | Verified | `test_deploy_integration` and e2e: pull by digest from a registry, platform and revision-label checks. |
| Native Linux binary deployment through the same lifecycle | Verified | systemd suite 01-13: upload, verification, confined unit, env quoting through systemd's parser, journald logs, restart, limits, rollback. |
| Successful deployments report commit, artifact, URL, readiness | Verified | `release`, `probes` and `url` in the deploy result (`test_commands`, e2e 07). |
| Logs, status, explicit rollback, interrupted-job recovery | Verified | e2e 07/12/13, systemd 09-11 (client dies; worker is `SIGKILL`ed; `recover` restores). |
| Failed releases return nonzero and preserve or restore the last good release | Verified | Docker lifecycle and e2e 09 (exit 5); systemd 06/07 (including a kernel OOM kill). |
| CI key creation, repository configuration, safe rotation | Setup verified with the real `gh` and GitHub; rotation against a stand-in `gh` | Real server run below: `github setup` against a private repository (secret, variables, key proven over Tailscale SSH). `test_github`; e2e 16-20 for wrong host keys and rotation. |
| A real GitHub push completes the full path through Tailscale/OpenSSH | Verified on a real server | Real server run below: a merge to `main` built the binary on a GitHub-hosted runner, joined the tailnet through OIDC, and deployed with the CI key (`gh-40839230420-2`). |
| No personal private key is uploaded; host trust is pinned | Verified | `test_github` (the secret is the dedicated key; no key material in results, state, or arguments); CI mode ignores `~/.ssh/config` (found and fixed by e2e 19); host keys come over the trusted connection. |
| Deployment privileges scoped to the registered application and helper | Verified | Real sudo/sshd: `test_host_integration` (account cannot write its own access, cannot use admin), e2e 15 and 24 (each login refused for the other application). |
| Secret values absent from repository files, normal output, artifacts | Verified for the paths tested | Env, registry token, CI key, sudo password: never in argv, results, state, or errors (`test_provision`, `test_commands`, `test_github`, `test_client`). The generated workflow passes secrets only through `env:`. |
| Resource limits, finite timeouts, log limits, retention active | Verified | Container memory/log/pids/caps flags and `MemoryMax` enforced by the kernel; readiness, command, lock, and upload deadlines; job pruning; image/binary retention including failed candidates. journald's own size limits are the host default. |
| Required local, Linux, failure, and live-workflow tests pass with recorded evidence | Partly | All but the live workflow, as above. |
| README and AGENTS.md let a fresh agent release and interpret failures | Written, not field-tested | `README.md`, `AGENTS.md`, the generated `SHIPLORD.md`; no fresh agent was run against them. |
| No additional continuously running deployment service | Verified by construction | Only jobs run, as transient units that exit; there is no daemon. |

## Plan §12C failure table

| Failure | Evidence |
| --- | --- |
| SSH unavailable | `test_client` (timeouts, brief failures while polling); nothing is sent, so nothing changes |
| Host key changed | e2e 18 (fails closed); `StrictHostKeyChecking=yes` asserted in every command |
| Deployment key revoked | e2e 19 (revoked CI key refused, no fallback because pinned mode is hermetic) |
| Missing sudo permission | `test_client` (messages), e2e 14/15; no partial mutation: validation precedes every side effect |
| Bad digest, missing image, registry denial | Docker lifecycle (missing digest, wrong revision): old container untouched |
| Wrong architecture or checksum | `test_payload`, `test_native`, systemd 08 |
| App exits or never becomes ready | Docker lifecycle, systemd 06; crash loops are classified as `exited` (found by the real run) |
| First release fails | `test_deploy` (no runtime, no route). **Not exercised with a real runtime.** |
| Caddy validation/reload failure | `test_routing`; real Caddy: stopped Caddy, invalid existing config |
| Invalid origins/configuration | `test_manifest`/`test_cli`, helper rejection tests |
| Disk full during staging/state write | `test_payload` (patched free space), `test_runtime` (patched), `test_release_state` write failures. A real full disk was **not** produced. |
| Concurrent deployments serialize | `test_release_state` (cross-process lock, killed holder), `test_helper_actions` (busy lock). **No two real concurrent deploys were run.** |
| Older queued deployment | `test_deploy`, e2e 10 |
| SSH disconnect during deployment | systemd 09 (client killed; the transient unit finishes) |
| Helper killed mid-deploy | systemd 10 (worker `SIGKILL`ed; journal; `recover`), Docker lifecycle (simulated kill) |
| Incompatible helper version | `test_protocol`, `test_helper_actions`, `test_provision` |
| Private-key upload to GitHub fails | `test_github` (setup resumes; rotation leaves the old key working) |
| Rollback itself fails | `test_deploy` (exit 5, previous restored; exit 6 when restoration fails) |

A real machine reboot was not tested and must not be done to your server for testing; behavior after power loss is unverified.

## What running it for real found

Each of these passed its unit tests first:

- sshd rejected `PermitUserEnvironment` inside a `Match` block.
- `useradd` forbids a colon in the comment field, which the account marker used.
- Provisioning validated the application settings only after creating the account.
- The environment-file parser split lines on more characters than Docker does (`\r`, `\x0b`, `\u2028`), so the two could disagree about a line.
- `check_public_https` verified the certificate against the dial address instead of the public name.
- A crash-looping container reports `Running=true`, so a crash was reported as a readiness timeout.
- After a crash loop, systemd's start-rate limit blocked the next `restart` (and so a rollback).
- Failed and interrupted candidates leaked their image or binary directory.
- Bootstrap left a root-owned `__pycache__` that blocked cleanup of its upload.
- `env set` with an empty file silently wiped the environment.
- CI-style SSH silently used `~/.ssh/config`, so a revoked CI key still "worked" via the personal key, and the setup check could pass on the wrong key.
- The test harness itself hid failures behind a pipe to `tail`.

## Real server run (Hetzner, 2026-10-28/29)

An Ubuntu x86-64 VM reached over Tailscale, behind Cloudflare's proxy with a firewall that admits only Cloudflare.
Application `hello`: a native Go binary, `hello.lordpatil.com`.

| What | Result |
| --- | --- |
| `server bootstrap`, `provision` with password sudo | Succeeded. |
| Native deploy, repeated deploy, `rollback` | v1 → v2 → back to v1; the repeat was `unchanged`, the rollback reused the retained binary. |
| Failed release (`/ready` returns 503) | Exit 5, `rolled_back`, previous release restored and serving; Caddy untouched. |
| Public HTTPS | Let's Encrypt certificate issued through Cloudflare's proxy; `https.verified: true`, `public.ok: true`. |
| `github setup` (real `gh`, private repository) | CI key registered, proven, stored; variables set. Found a bug first (below). |
| GitHub Actions deploy | First attempt refused by Tailscale (below); after correcting the subject, the run deployed v4 by itself. |

What it found:

- `github setup` proved the CI key against the administrator's SSH alias, which CI's hermetic SSH cannot resolve. Fixed:
  the proof uses the resolved host, as CI does (also in `rotate-key`).
- GitHub repositories with immutable OIDC subjects present `repo:Owner@<id>/name@<id>:...`, so a trust credential written
  for `repo:Owner/name:...` is refused with a bare 403. `github setup` now reads the real subject from GitHub and prints it.
- `caddy validate` ran without `HOME`, so Caddy wrote its storage to `./caddy` in the working directory: a stray `/caddy`
  on the real server, and a failure for non-root users (GitHub's runner). Validation now gets a throwaway home.
- On GitHub's runner, `userdel` refused the deployment account while a finished SSH session's user manager lingered.
  `deprovision` now ends the account's sessions and retries.
- Readiness only checks for HTTP 200, so an application whose catch-all handler answers `/ready` passes. Expected, and
  documented: the ADK template's `/ready` checks its database.

## Deviations from the plan

- **The compose host port is not made configurable.** The plan asked for it, but Shiplord never uses the template's
  `compose.yaml`: the helper runs the container and the server allocates the port. The template's own tests assert on
  that file, so editing it would risk the generated project's CI. The overlay is add-only (`SHIPLORD.md`).
- **Release ordering** uses the commit's committer timestamp, so CI and local deploys agree; `--sequence` overrides.
- **Administrator operations** send the sudo password on stdin (never argv) after `sudo -n` reports it is needed,
  rather than allocating a TTY; requests are frames, so secrets never touch argv.
- **Architectures:** native binaries may be `linux-amd64` or `linux-arm64`; the server accepts only its own.
- **Names:** repository variables and secrets use a `SHIPLORD_` prefix instead of `SHIP_`.
- **Workflow generation** covers Docker applications. A native application gets the CI key, secret, and variables, but
  you call `shiplord deploy --binary` from your own build job.
- **`doctor --server`** is a cheap protocol probe by default; `--deep` adds the sudo-backed health report.
- **Registry pull logins** are per application, read-only, and stored root-only; only `ghcr.io` is accepted.

## Configurable Docker storage (PR 1)

- Unit: `tests/test_storage.py` (path/ownership validation, legacy records, plan, negotiation), `tests/test_runtime.py`
  (mount path, volume provenance, container identity).
- Real Docker (`tests/linux/run.sh docker`): `test_storage_path_changes_keep_one_volume_and_its_files` writes a counter
  and SQLite rows at the legacy path, redeploys the same image/commit/sequence with `/data`, rolls back a failing
  candidate that asked for another path, and returns to the legacy path; one volume throughout.
- Not done: ownership initialization (`owner_uid`/`owner_gid` are rejected by the helper), the non-root UID 10001
  fixture, interruption tests around volume creation, and proof that the path-only restoration regression fails on the
  baseline in real Docker (the unit test shows it at the `is_running` boundary). Unlabelled legacy volumes are
  accepted by name rather than by evidence of a retained container.

## Graceful Docker replacement (PR 2)

- Unit: `tests/test_shutdown.py` (scripted Docker with a fake clock: exit in time, force, fail, late exit, continuing attempt,
  ambiguity, malformed metadata, legacy containers, signals, daemon errors, paused, disappearing/unkillable containers,
  transaction and retirement behavior, budget, negotiation).
- Real Docker (`tests/linux/run.sh docker`): signal-aware PID 1 flushes a SQLite row and file before the candidate starts;
  an ignoring process is force-killed after its deadline; with `fail` the original keeps running, recovery refuses while it
  drains and succeeds after it exits.
- Not done: killing a real worker at each point in the stop sequence, the marked real 120-second drain test (short delays
  used), persisted stop deadline across workers, SIGKILL-race around `docker kill` crash gap, rollback/failed-first-deploy
  cleanup against real Docker, a seeded-baseline demonstration of the regression.

## Not verified, and why

- The plan's controlled installation procedure (§12E) on your production server; the real server run above used a separate VM.
- The Docker runtime on a real server (verified in containers only), and the real ADK application's CORS.
- `env set`, `recover`, `deprovision --retire` and `rotate-key` on a real server (verified in containers).
- Private registry authentication end to end (the login file is accepted by the real `docker --config`; a registry that
  demands credentials was not run).
- The real ADK application's CORS, readiness under load, and memory needs: the 512 MiB default is an example, not a measurement.
- x86-64 for the real-host container suites: they ran on arm64 VMs; GitHub's x86-64 runners run them in this repository's CI.
- CI installs Shiplord's own dependencies unpinned (`pip install -e .`); a new release of one could change CI results.
- Native services can still connect to other loopback services (only binding is restricted), and Docker applications share
  the default bridge network; see the README's *Limits of the confinement*.
- Behavior across reboot or power loss; two truly concurrent deployments; a genuinely full disk.
