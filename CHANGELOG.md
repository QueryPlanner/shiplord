# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-11-13

### Upgrade

- After installing `shiplord==0.3.0`, run `shiplord server bootstrap <alias>` on
  each server to install the matching helper. Run `shiplord github setup` to
  regenerate workflows with the new CLI pin.

### Added

- Add `shiplord release` to build and push a Docker image from a clean checkout,
  then deploy the pushed digest without copying it by hand.
- Add `shiplord setup`, which runs configure, provision, env, registry login,
  github setup, and check-ci in order and resumes at the first incomplete step.
- Render every command's result as readable lines, and accept `--json` on
  `init`, `configure`, and `server add` as the other commands already did.
- Add a `QueryPlanner/shiplord/deploy` composite action for the generated Docker
  workflow's tailnet, SSH, deploy and summary steps.
- Add `shiplord recover <app> --server <alias>`, so an interrupted deployment can be
  reconciled from any directory without a checkout, and print that target-specific
  command in human-readable `status` output.
- Add `shiplord server remove` and `shiplord server rename`, both with `--json`.
  Removal refuses while the current directory's `ship.toml` names the alias.
- Add `shiplord provision --update` to apply a changed hostname or memory
  ceiling without removing the application's state, environment, or keys.
- Serve additional hostnames per application: `http.hostnames` (or
  `--hostname`, repeatable) and `http.www_redirect` (`--www`) redirect
  permanently to the canonical `http.api_domain`, keeping the path and query
  string. The extra hostnames are part of the registration, so a deployment
  cannot add or drop them.
- Generate a GitHub Actions release workflow for native applications: it builds
  the binary, uploads it as an artifact, and deploys it with
  `shiplord deploy --binary`. `github setup` takes `--build-command` and
  `--binary-path` and records both in `[github]`.
- Restart the current release with `shiplord restart`, for example after an
  environment change. Use `shiplord logs --follow` to follow its output.
- Recover interrupted deploys at boot with a systemd unit that defers to any
  active job.
- Back up and restore persistent application data. Archives stay on the server
  under `/var/lib/shiplord/backups/<app>` and need a separate off-host copy.
- Create and reuse the owner-wide Tailscale trust credential during `github
  setup` when `TAILSCALE_API_KEY` is set. Configure a deployment branch other
  than `main` and use a registry other than GHCR.
- Set, unset, and preview environment changes with `env set`, `env unset`, and
  `env diff`. Configure CPU and readiness settings in `ship.toml`.
- Check all registered servers with `doctor --all-servers` and look up past jobs
  with `status --job-id`.

### Changed

- Require Ubuntu 24.04 or later or Debian 12 or later on the server. Bootstrap
  checks this before installing the helper.

### Fixed

- Detect helpers older than 0.2.0 before requesting app inventory and show the
  bootstrap command. For this confirmed incompatibility, `shiplord apps` now
  returns exit code 4 and `helper_upgrade_required` instead of exit code 2 and
  `invalid_request`. Update scripts that match the previous values.
- Preserve the real rollback target when redeploying the current release.
- Capture a failed Docker candidate's logs before restoring the previous release.

## [0.2.0] - 2026-11-08

### Added

- Add server application inventory with partial all-server results.
- Add named application targets for status and logs.
- Group command help and render concise inventory, release, and doctor output.
- Add actionable recovery and server authorization errors.
- Report client executable and helper versions in diagnostics.

### Changed

- Publish the CLI and server helper as version 0.2.0.

## [0.1.0] - 2026-11-06

### Added

- Create and configure applications with the Google ADK template or a generic Docker profile.
- Register servers, bootstrap a privileged helper, and provision isolated application accounts.
- Deploy Docker images and native Linux binaries with readiness checks and Caddy routing.
- Manage runtime environment values, registry credentials, logs, status, rollback, and recovery.
- Generate GitHub Actions release workflows with Tailscale OIDC and an access-only check.
- Configure persistent Docker storage and graceful container replacement.
- List registered servers and interactively configure applications.

### Fixed

- Generate valid GitHub Actions branch expressions for release workflows.

[Unreleased]: https://github.com/QueryPlanner/shiplord/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/QueryPlanner/shiplord/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/QueryPlanner/shiplord/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/QueryPlanner/shiplord/releases/tag/v0.1.0
