# Working on and with Shiplord

## If you are releasing an application with Shiplord

Use only the `shiplord` commands below. Never SSH to the server to run Docker, systemctl, Caddy, or sudo yourself,
and never edit files under `/etc/shiplord`, `/etc/caddy/ship`, or `/opt/shiplord`: the helper owns them.

Release, from the application's directory (it contains `ship.toml`):

```sh
shiplord doctor --json                                   # prerequisites, SSH access, helper, DNS
shiplord plan --image <registry/name@sha256:digest> --commit <40-hex sha> --json   # read-only
shiplord deploy --image <registry/name@sha256:digest> --commit <40-hex sha> --json
shiplord status --json
```

Native binaries: `shiplord deploy --binary <path> --commit <sha> --json` (a Linux ELF for the server's CPU).

Read the exit code and the JSON `status`/`error`:

| Exit | `status` / `error` | Meaning and next step |
| --- | --- | --- |
| 0 | `succeeded`, `unchanged`, `already_applied` | Done. Report `release.commit`, `release.artifact`, and the API hostname from `ship.toml`. |
| 1 | `stale_release` | A newer release is deployed. Do not force it; ask before `--allow-older`. |
| 1 | `recovery_required` | An earlier job was interrupted. Run `shiplord recover`, then retry. |
| 1 | `prepare_failed` | The artifact was refused (pull, platform, revision, checksum). Nothing changed. Fix it. |
| 2 | `invalid_request` / `rejected` | Fix the input; the message says what. Do not retry unchanged. |
| 3 | access / sudo / SSH | A prerequisite is missing. Run `shiplord doctor --json` and report the failing check. |
| 4 | incompatible protocol | The CLI and server helper versions differ. Re-run `shiplord server bootstrap`. |
| 5 | `rolled_back` | The release failed and the previous one is serving. This is a failure: report it, show `logs`. |
| 6 | `intervention_required`, `interrupted` | Run `shiplord recover`. If it also exits 6, stop and report. |

To retry the same deployment safely, repeat the command with the same `--request-id`. To go back deliberately, use
`shiplord rollback`. To report what is deployed: `shiplord status --json` gives the commit, artifact, running state,
and any unfinished transaction. Never claim a release is live unless `deploy` exited 0.

Secrets: set runtime environment with `shiplord env set --from-file <file>` (use `-` for stdin). Values are never
printed or logged; do not put them in `ship.toml`, commits, or command-line arguments.

## If you are changing Shiplord

Use Python 3.11 or later. Install with `python -m pip install -e .`. Keep the CLI entry point named `shiplord` and
the manifest named `ship.toml`.

Tests:

```sh
python -m unittest discover -s tests            # unit tests (real-host suites are skipped)
tests/linux/run.sh <unit|caddy|host|docker|e2e|systemd|all>   # real sshd/sudo/Caddy/Docker/systemd in containers
```

Rules that have already paid for themselves:

- **Run the real-host suite that matches what you changed.** Privileged, runtime, routing, SSH, and systemd code has
  repeatedly been wrong in ways fakes could not show (an illegal sshd directive, a colon in a passwd comment, a
  systemd start-rate limit, an env-file parsing difference). Fakes cover control flow; the Linux suites cover truth.
- **Every module the server runs must be stdlib-only.** `installer.CLIENT_MODULES` lists the client-only modules; a
  test enforces the rest. A new module is shipped to the server automatically, so keep its imports light.
- **Validate before the first side effect.** Reject bad input before creating accounts, files, or connections.
- **A restore is only a restore once it is verified.** Never clear the journal before the previous release has
  been started, checked ready, and routed. Never report success you did not verify.
- **Never put values in errors.** Secrets, environment values, and host paths must not reach messages shown to a
  deployment account or to the client.
- **Privileged state is written with `dir_fd`, `O_NOFOLLOW`, an owner check, and fsync ordering.** Reuse the helpers
  in `release_state.py`; do not open privileged paths with plain `open`.
- **Do not install or test a privileged helper against a production server** before the Linux suites pass. The
  suites are the disposable-host evidence the plan requires.
- Do not put secrets in manifests, output, tests, or generated files. Tests use throwaway keys and fixtures.

`ship-cli-plan.md` is the design; [docs/evidence.md](docs/evidence.md) records what each Definition-of-Done item is
verified by, and what is not yet verified.
