# Operating Shiplord

## Host layout (created by `shiplord server bootstrap`)

| Path | Owner / mode | Purpose |
| --- | --- | --- |
| `/usr/local/sbin/shiplord-host` | root 0755 | Helper reached through sudo by one application account. |
| `/usr/local/sbin/shiplord-admin` | root 0755 | Administration entry point (bootstrap-installed, admin sudo only). |
| `/usr/local/sbin/shiplord-worker` | root 0755 | Runs a queued job; started by the helper as a transient unit. |
| `/usr/local/libexec/shiplord-dispatch` | root 0755 | Forced command for SSH logins. |
| `/usr/local/lib/shiplord/<version>/` | root 0755 | Immutable helper code; `current` is switched atomically. |
| `/etc/shiplord/apps/<app>.json` | root 0600 | Registration: account, runtime, hostname, image repository, host port. |
| `/etc/shiplord/authorized_keys/<account>` | root 0644 | Forced-command key lines (sshd reads it as the account). |
| `/etc/shiplord/env/<app>/app.env` | root 0600 | Runtime environment (`shiplord env set`). |
| `/etc/shiplord/docker/<app>/config.json` | root 0600 | That application's read-only registry login (`shiplord registry login`). |
| `/etc/ssh/sshd_config.d/50-shiplord.conf` | root 0644 | Confines `ship-*` accounts: no TTY, no forwarding, key-only. |
| `/etc/sudoers.d/shiplord-<app>` | root 0440 | Lets one account run the helper, nothing else. |
| `/etc/caddy/ship/*.caddy` | root 0644 | One generated route per application, imported by the main Caddyfile. |
| `/var/lib/shiplord/apps/<app>/` | root 0700 | `state.json`, `journal.json`, staged uploads. |
| `/var/lib/shiplord/jobs/<app>/` | root 0700 | Job records (finished records beyond the newest 25 per application are pruned; unfinished ones are kept). |
| `/var/lib/shiplord/data/<app>/` | runtime 0700 | Writable data of a native application. |
| `/opt/shiplord/<app>/<release>/app` | root 0755 | Versioned native binaries. |

Bootstrap adds exactly one line to the main Caddyfile (`import /etc/caddy/ship/*.caddy`) after validating it,
and never rewrites anything else in it.

## Accounts

- `ship-<app>`: the deployment account (for long names, a shortened name with a hash). Its shell is only ever
  used to run the forced command.
- `app-<app>`: the runtime account of a native application. It cannot log in.

## Checking a server

`shiplord doctor --server production --deep` (asks for the sudo password if needed) reports, read-only: the helper
installation, whether Caddy imports the managed routes and is running, whether the sshd drop-in is current and sshd
accepts its configuration, whether Docker answers, free disk on the Shiplord paths and Docker's, and each
application's state including any unfinished transaction.

## Failure and recovery

| Situation | What happens | What you do |
| --- | --- | --- |
| Image cannot be pulled, wrong platform, wrong revision | Nothing changes; exit 1. | Fix the artifact. |
| New release never becomes ready or exits | Previous release restored; exit 5. | Read `status`, `logs`. |
| First release fails | No runtime, no route; exit 1. | Fix and redeploy. |
| Caddy rejects the route | The previous file is restored and Caddy reloaded; exit 5. | Check the hostname and Caddy logs. |
| Worker or host dies mid-deployment | Journal stays; deploys refused with `recovery_required`. | `shiplord recover`. |
| Restoration itself fails | Exit 6; journal stays. | `shiplord recover`, then investigate. |
| Client disconnects | The job continues. | `shiplord status`. |

Storage: the application volume is never removed by deploy, rollback, recover, retirement or retention. If a deploy is
refused with phase `storage`, the volume has another driver, driver options, another application's label, or no retained
Shiplord container proving that an unlabelled legacy volume belongs to this application. Inspect it with
`docker volume inspect` as an administrator and resolve it deliberately. A running container is recognised only when
its image digest, application profile, shutdown policy, and exactly one Shiplord volume mount match the release.
Extra image-declared mounts are allowed, so an image `VOLUME` instruction does not make a healthy release appear absent.

Generic Docker applications use `template = "generic"`; Shiplord still owns the volume name and port binding.
The application image owns its process startup, CORS, and any website routes. Shiplord supplies no upload or
generation rate limits. Keep any existing traffic protections when moving an application behind the Shiplord route.
The generated GitHub workflow supports only ADK; generic applications need their own CI workflow.
After recording a generic release, retain a helper that supports `docker_generic_v1` for status, rollback,
and recovery. An older helper cannot interpret the generic release record.

An unfinished transaction may include a legacy-volume proof or a shutdown record in its journal. Recover that
transaction before downgrading the helper, because an older helper cannot read these added journal fields. Older
journals without these fields remain readable.

Shutdown intent and each stop step are synced to the transaction journal before Shiplord changes restart policy, sends
a signal, or sends `KILL`. Recovery uses the saved container ID and deadline. A crash after Docker accepted a signal but
before Shiplord recorded that result can cause recovery to send that signal again. Stop signals therefore have
at-least-once delivery across worker crashes.

A `fail`-policy deadline exits 6 with `kind: shutdown_timeout`; Shiplord never sends `KILL` for that policy. The original
container keeps running with its restart policy set to `no`, and the journal remains available. After it exits,
`shiplord recover` restores the committed release. If recovery reports `shutdown_timeout`, the process is still draining.

A real reboot is not part of the automated tests. Docker containers use `--restart unless-stopped` and native
units are enabled, but behavior after a power loss has not been verified.

## Troubleshooting

- `access denied`: the application is not provisioned for your key, or `provision` has not run.
- `recovery_required`: an earlier job was interrupted; run `shiplord recover`.
- `stale_release`: a newer release is already deployed. Use `rollback` or `--allow-older` deliberately.
- Public HTTPS check fails but the deploy succeeded: DNS or certificate issuance has not settled; `doctor` shows
  which. The server-side result separates local readiness, proxy routing, HTTPS, and DNS.
- Disk is tight: only Shiplord-owned images and releases are ever removed; nothing else on the host is pruned.
