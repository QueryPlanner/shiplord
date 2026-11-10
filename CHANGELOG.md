# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Generate GitHub Actions workflows that install `shiplord==<version>` from PyPI by
  default. `--tooling-ref` still installs a commit, branch, or tag from GitHub.

### Fixed

- Detect helpers older than 0.2.0 before requesting app inventory and show the
  bootstrap command. For this confirmed incompatibility, `shiplord apps` now
  returns exit code 4 and `helper_upgrade_required` instead of exit code 2 and
  `invalid_request`. Update scripts that match the previous values.

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

[Unreleased]: https://github.com/QueryPlanner/shiplord/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/QueryPlanner/shiplord/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/QueryPlanner/shiplord/releases/tag/v0.1.0
