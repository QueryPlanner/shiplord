# Ship CLI: implementation plan and acceptance criteria

Status: proposed design; no CLI or server helper has been implemented or installed.
Prepared: 2026-10-27.

## 1. Outcome and development split

An AI coding agent or developer can create an application from the existing ADK
template, configure its API hostname and frontend origins, provision it once,
and ship tested releases through GitHub Actions. The same deployment operations
are available from a local terminal.

Build and test the CLI on the MacBook. Develop the helper in the same tooling
repository and use GitHub-hosted ubuntu-24.04 runners for disposable Linux system
tests. GitHub creates and disposes of those VMs; the user does not need to own,
rent, or configure a separate Ubuntu VM. After those tests pass, install and
integrate the helper on this server. Keep the CLI, helper, protocol, tests, and
documentation versioned together. Keep application code in application repositories.

The tooling introduces no continuously running application service. It uses the
existing OpenSSH, Tailscale, Docker, systemd, and host Caddy services. A deployment
may create a temporary systemd job so that an SSH disconnect does not terminate
an in-progress operation.

## 2. Confirmed environment and constraints

- Server: Ubuntu 24.04, x86-64, approximately 4 GB RAM.
- Tailscale hostname: ubuntu-4gb-hel1-1; DNS name:
  ubuntu-4gb-hel1-1.taildd3f7a.ts.net.
- Existing administrator account: appuser. Noninteractive sudo was unavailable
  during inspection; bootstrap needs an authenticated interactive sudo session
  or installation by an administrator.
- Caddy runs directly on the host as a systemd service and reads
  /etc/caddy/Caddyfile. It already serves several applications.
- Docker and Tailscale are running. Preserve existing service configurations.
- Approximately 9 GB of disk was free when inspected. Retention and bounded logs
  are first-version requirements. Do not prune resources belonging to other apps.
- Local template checkout: /home/appuser/google-adk-on-bare-metal, revision
  6631209 when inspected, with an existing modification to compose.yaml.
  Do not overwrite that checkout or its changes.
- Template source: https://github.com/QueryPlanner/google-adk-on-bare-metal.
  Its current GitHub version is newer than the inspected local checkout.

The template already has application configuration for ALLOW_ORIGINS, /live and
/ready endpoints, Docker Compose, persistent artifact storage, quality checks,
GHCR image publishing, and a manually triggered deployment workflow. Recheck and
pin its exact commit when implementation starts.

## 3. First-version scope

Included:

- macOS CLI, including Apple Silicon; Python 3.11+ is the initial implementation
  choice, using the standard library where practical.
- OpenSSH transport over Tailscale, not Tailscale SSH.
- First-class Docker deployment for the ADK template and a native Linux binary
  adapter managed by systemd.
- Application provisioning, host-port allocation, Caddy routing, runtime
  environment management, health checks, release state, recovery, and rollback.
- GitHub Actions generation/configuration using the existing template checks
  and image publishing foundation.
- Automated repository-specific SSH credential setup and rotation.
- Explicit frontend-origin configuration using the existing ALLOW_ORIGINS input.
- Agent-friendly output, meaningful exit codes, documentation, and tests.

Excluded:

- Application authentication, authorization, user management, and agent cost
  controls. Infrastructure SSH and sudo permissions remain necessary.
- A dashboard, deployment API, Kubernetes, or a persistent deployment daemon.
- Zero-downtime deployments, preview environments, multi-server orchestration,
  and arbitrary user-supplied root scripts.
- Automatic public DNS changes in version one. The CLI checks DNS and explains
  the required record; the user creates it at their DNS provider.
- Automatic database provisioning, destructive migrations, or database rollback.

## 4. User-facing command contract

Names below are proposed interfaces, not existing commands.

### Once per local machine

    ship doctor --local
    ship server add production --ssh appuser@ubuntu-4gb-hel1-1

`server add` records a local server alias and establishes server trust. It uses
the user's existing SSH configuration and identity. It does not replace personal
SSH keys or copy a personal private key to GitHub.

### Once per server

    ship server bootstrap production
    ship doctor --server production

Bootstrap installs versioned, root-owned helper/dispatcher files, creates managed
directories, adds the Caddy import, and prepares sudo policy. It uses interactive
administrator access. Re-running it is safe and does not erase existing routes.

### Once per application

    ship init orders --template google-adk
    cd orders
    ship configure --server production \
      --api-domain orders-api.lordpatil.com \
      --frontend https://orders.lordpatil.com
    ship provision
    ship github setup --repo QueryPlanner/orders
    ship doctor

`init` creates a new project from a pinned template revision and refuses to
overwrite a nonempty directory. `configure` persists nonsecret choices.
`provision` allocates server resources and installs the selected local user's
public key for application operations. It uses administrator access only for
this setup. The server remains authoritative for allocated ports and app identity.

### Routine use

    ship plan
    ship deploy --image ghcr.io/queryplanner/orders@sha256:<digest>
    ship deploy --binary ./dist/orders --commit <commit-sha>
    ship status
    ship logs --tail 100
    ship rollback
    ship recover
    ship github status
    ship github rotate-key

`deploy` receives an already built artifact; it does not compile on the server,
commit code, or push Git changes. CI uses this same deployment interface. The
normal automatic release trigger is a successful push to main.

`plan` reports intended changes without mutating the server. `recover` reconciles
an interrupted transaction to the last committed release, with a clear result.
`rollback` restores the preceding successful artifact and managed nonsecret
configuration, checks readiness, and keeps the current secret values. Neither
operation rolls back a database or persistent application data.

Provide `--json` for plan, doctor, deploy, status, rollback, recovery, and GitHub
status. Do not emit private keys, secret values, or complete environments.

### Exit codes

- 0: requested operation succeeded; a deployment is verified ready.
- 1: operation failed; inspect the structured error/result.
- 2: invalid arguments or configuration.
- 3: prerequisite, SSH trust, credentials, or permissions missing.
- 4: incompatible CLI/helper protocol.
- 5: deployment failed and the previous release was successfully restored.
- 6: recovery failed or manual intervention is required.

A failed release must return nonzero even when rollback succeeds.

## 5. Application configuration

Example proposed ship.toml:

    schema_version = 1
    name = "orders"
    server = "production"
    template = "google-adk"
    runtime = "docker"

    [http]
    api_domain = "orders-api.lordpatil.com"
    frontend_origins = ["https://orders.lordpatil.com"]
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

Resource values are initial configuration examples, not measured requirements.
Tune the ADK application's memory limit through a representative smoke test.

Resolve a local alias into the full server hostname and app-specific SSH user
when configuring CI. Do not require GitHub runners to have local alias files.
Store no private keys, application secrets, or registry credentials in ship.toml.
Validate names, hostnames, ports, paths, sizes, and origin syntax. Frontend origins
are explicit HTTP(S) origins without paths or credentials. Production defaults
exclude localhost and wildcard origins. No frontend means an explicit empty list.

Server registration fixes each application's identity, runtime, allowed image
repository, hostname, port, and runtime policy. Routine deploy requests cannot
silently change those settings. Such changes use a provisioning/update operation
with administrator authority and a reviewable plan.

## 6. SSH, Tailscale, and GitHub setup

### SSH trust and login

- Bootstrap uses an already trusted personal OpenSSH connection. If no trust
  exists, require a fingerprint verified against the server console or another
  trusted channel; do not silently trust ssh-keyscan output.
- Record the server's public host key for the exact hostname used by CI, including
  port notation if a nondefault SSH port is selected. Host private keys stay on
  the server. A changed host key fails closed until deliberately re-verified.
- Provision a separate deployment login per application, for example ship-orders.
  Native application processes use a separate runtime account.
- Authorized keys invoke a root-owned dispatcher with an application-bound
  forced command and restrict forwarding and PTY access. The dispatcher parses
  allowed operations and never evaluates SSH_ORIGINAL_COMMAND as shell code.
- The public-key file, dispatcher, helper, and their controlling directories are
  protected against modification by the deployment account.
- The sudo helper independently binds requests to the calling deployment account
  and its registered application. Do not rely only on a client-provided app name.
- Sudo permits the fixed helper entry point, not arbitrary shells, Docker CLI
  commands, systemctl commands, or uploaded scripts.

### GitHub credential configuration

`ship github setup` uses the local authenticated gh CLI to:

1. Check access to the intended repository and report missing permissions.
2. Generate a dedicated Ed25519 CI key in a protected temporary directory.
3. Install the public key for this app using administrator access and test it.
4. Set SHIP_SSH_PRIVATE_KEY as an Actions secret using standard input.
5. Set SHIP_SERVER, SHIP_USER, and SHIP_SSH_KNOWN_HOSTS as repository variables.
6. Configure the Tailscale client ID/audience references and generate the workflow.
7. Report what remains before the workflow can run; never claim a live CI test
   passed until a GitHub run actually succeeds.
8. Remove temporary private-key files after successful setup. On partial failure,
   retain enough protected local state to resume or revoke the unused key.

Use the existing local SSH identity for local deployments after its public key
is registered for the app. CI has its own key. Adding another local machine means
registering that machine's public key, not distributing the CI private key.

Setup is resumable and does not overwrite a working CI key on every invocation.
Rotation installs the new public key, updates GitHub, verifies it, and only then
removes the old key. Record key fingerprints, not private key material.

### Tailscale runner enrollment

Use GitHub OIDC and tailscale/github-action with a pinned version/commit. A
Tailscale federated identity trusts the intended repository and main-branch job
claims and grants an application-appropriate CI tag. Network policy permits that
tag to reach this server's OpenSSH port. Tailscale SSH is not required or enabled.

The initial trust configuration requires Tailscale administrative authority. A
logged-in local Tailscale device is not, by itself, a management API credential.
Version one can guide that one-time setup; it must not promise automatic creation
without supported API permissions. If automation is added, use explicit scoped
administrative credentials and preserve existing tailnet policy.

## 7. CLI/helper protocol and host layout

Agree and test this interface before installing a privileged helper:

- A fixed OpenSSH command reaches the dispatcher; structured requests travel over
  stdin. Native binary payloads also stream over stdin with a declared byte count
  and SHA-256 digest. Do not depend on unrestricted scp/SFTP when forced commands
  are in use.
- Include protocol version, request/job ID, action, application ID, commit SHA,
  and typed action-specific arguments. Enforce size and timeout limits.
- Docker deployments accept an allowlisted image repository plus immutable
  sha256 digest. Native deployments verify payload size, checksum, architecture,
  and executable format before execution. Never extract arbitrary archive paths.
- Return structured status, release identity, phase, error code, readiness result,
  and recovery result. Commands must propagate remote failures accurately.
- Provide a version/capabilities handshake and reject incompatible versions before
  mutations. Use an idempotency key for deployment submissions.
- The helper uses controlled executable paths and environment, validates every
  input, and never executes a client-supplied command line through a shell.

Proposed root-owned layout:

    /usr/local/sbin/ship-host
    /usr/local/libexec/ship-dispatch
    /etc/ship/apps/orders.json
    /etc/ship/env/orders/...
    /etc/caddy/ship/orders.caddy
    /var/lib/ship/apps/orders/releases/...
    /var/lib/ship/apps/orders/state.json
    /var/lib/ship/jobs/...
    /run/lock/ship.lock

Use a host-wide mutation lock for the first version, serializing deploys and
Caddy changes. Read-only status remains available while a job is running.
State files use atomic writes and an explicit transaction journal. Stream uploads
directly into root-controlled staging files; do not trust user-writable symlinks
or paths passed to privileged operations.

## 8. Deployment and recovery behavior

Version one accepts a brief restart window to limit simultaneous RAM use.

1. Check protocol, registered app, lock, disk space, runtime, and artifact identity.
2. Pull the Docker image by digest, or receive and verify the native binary.
   Preserve the running app if preparation fails.
3. Persist the deployment request and transaction state. Start a transient
   systemd job so disconnection of the initiating SSH session does not kill it.
4. Stop the old instance; start the candidate on the app's fixed loopback port.
5. Poll /ready or the configured readiness path with a bounded deadline. Health
   checks do not invoke an LLM or other paid application operation.
6. For a first deployment or routing change, apply the Caddy transaction only
   after local readiness succeeds. For an unchanged route, skip the reload.
7. Verify routing over HTTPS and commit the successful release record. Distinguish
   local readiness, proxy routing, and public DNS verification in results.
8. On failure, restore the previous artifact/configuration, check its readiness,
   restore routing if needed, and report the failed release with a nonzero code.
   If no preceding release exists, remove the failed runtime and restore the
   prior Caddy state.
9. Clean up older managed releases only after success. Retain the preceding
   successful rollback target and persistent application data.

The CLI can reconnect and poll the job ID. On process kill or server reboot,
unfinished journals remain visible. Do not report them as successful; recover
to the last committed release before accepting another deployment for the app.
Test this behavior rather than assuming systemd or restart policies supply it.

Docker applications get restart policies, memory limits, bounded logs, unique
Compose project names, and persistent per-app artifact volumes. The image listens
on 0.0.0.0 inside the container; its published host port binds to 127.0.0.1.

Native applications use root-owned versioned binaries and dedicated systemd units
with unprivileged runtime accounts, restart behavior, and memory limits. They
listen on 127.0.0.1 and accept HOST/PORT configuration. Build Linux x86-64 binaries
in Linux CI or with a tested compatible cross-compilation target, not as macOS
binaries. Keep writable application data outside release directories.

Registry access is necessary for private GHCR images. Provision a root-protected
read-only pull credential or a documented supported identity; never assume CI's
publishing token automatically grants the server permanent pull access.

## 9. Caddy transaction

Bootstrap adds this top-level line exactly once:

    import /etc/caddy/ship/*.caddy

Each managed route contains only that app's registered hostname and loopback
upstream. It does not replace or reformat the existing main Caddyfile.

Under the host mutation lock, back up the affected file, atomically install the
candidate, validate the complete configuration, and reload using the installed
Caddy service. Use the caddy service identity/environment for validation where
required by its configuration. If validation or reload fails, restore the prior
file and reconcile the active configuration. Do not restart Caddy to deploy.

Detect hostnames already owned by existing or managed routes and refuse takeover.
Automatic port allocation checks registered ports and current listeners. It is
serialized for managed apps, and startup failures still handle external port races.

## 10. Changes to the ADK template

Keep the template's current application structure and existing checks.

- Add ship.toml generation and document supported template/tooling versions.
- Extend initialization to replace application/repository identifiers safely.
- Make Compose's host port configurable; retain loopback binding, port 8080 inside
  the container, /ready health checking, and persistent artifact storage.
- Ensure the release path pulls the selected immutable image without rebuilding
  on the VM. Preserve an intentional local build path for development.
- Serialize frontend origins to ALLOW_ORIGINS as a JSON string. Use the existing
  server setting; do not add a second CORS layer in Caddy.
- Keep SERVE_WEB_INTERFACE=false and RELOAD_AGENTS=false in production.
- Store application secrets outside Git and build artifacts. Support explicit
  environment input over stdin/a protected file and correct runtime serialization;
  never source an uploaded environment file as shell code.
- Update the existing deployment workflow to call pinned shared tooling. Audit
  every job condition/dependency when enabling deployment on successful main
  pushes; the current manual dispatch conditions cannot simply be ignored.
- Reuse required quality checks and GHCR output digests. Keep existing optional
  evaluation policy separate from the deployment tool's responsibilities.
- Add agent instructions explaining test commands, release commands, recovery,
  expected output, and how to report the deployed commit and URL.

Keep shared implementation in one tooling repository. Pin it in generated
workflows. A clean GitHub runner must install/use that version reproducibly
without relying on local MacBook paths or aliases.

## 11. GitHub pipeline contract

Pull requests run the existing checks without VM deployment privileges. Main
branch releases run required checks, build/publish the image, join Tailscale,
install temporary SSH credentials/known_hosts, invoke the same deploy command,
verify the result, and clean up. Manual dispatch remains available.

- Grant id-token: write only where needed for Tailscale OIDC.
- Use an exact built image digest and exact checked-out commit SHA.
- Use StrictHostKeyChecking=yes, BatchMode=yes, and an explicit identity.
- Keep deployment concurrency serialized; do not cancel an active production
  deployment when another commit arrives.
- Prevent a queued older release from replacing a newer committed release.
  Record release ordering; deliberate older releases use the rollback interface.
- Re-run requests safely using request IDs and recorded image/config identity.
- A failed readiness check or successful automatic rollback still fails the job.
- Keep runtime secrets on the server unless explicitly managed otherwise.
- Configure finite workflow/job/health timeouts and meaningful status output.

## 12. Testing strategy

### A. Local tests on macOS and Linux

Use real parsers and filesystem behavior. Mock only external boundaries such as
OpenSSH, gh, Docker, and provider APIs when unit testing those integrations.

Cover:

- Valid and invalid manifests; origin serialization; configuration precedence.
- Template generation in new directories and refusal to overwrite existing work.
- Shell metacharacters, whitespace, option-like hostnames, and malformed inputs
  reaching process boundaries without command injection.
- Protocol negotiation, error propagation, JSON output, and exit codes.
- Key setup resume/rotation/revocation and partial GitHub configuration failure.
- Template output/workflow syntax, deterministic pinned tooling, and absence of
  secret values in generated files, logs, plans, and error messages.
- Generated Docker builds target linux/amd64 on Apple Silicon.

Run formatting, linting, type checking, and tests for the chosen CLI toolchain.
For modifications to the ADK template, obey its AGENTS.md and run its complete
required checks in the stated order.

### B. Real system integration on GitHub-hosted Ubuntu runners

Create a tooling-repository integration job with runs-on: ubuntu-24.04. Each job
gets a disposable GitHub-hosted VM and administrative access through sudo. No
additional VM or permanent server is required. This job uses normal GitHub Actions
minutes, subject to the account's included allowance and billing configuration.

Run the job directly on the VM, without a job-level container or ubuntu-slim.
Check prerequisites, install pinned Caddy/OpenSSH components as needed, and use
real sudo, Docker, systemd, and Caddy. Exercise CLI-to-helper transport through an
OpenSSH connection to 127.0.0.1 with throwaway keys and pinned test host keys.
Create a pre-existing fixture Caddy route to detect unrelated-route damage.

These tests need no production SSH secrets, Tailscale identity, real DNS, or
public certificates. Use loopback hostnames and local HTTP or a test CA explicitly
trusted by the test client. The final smoke test verifies real DNS/TLS/Tailscale.
Do not disable TLS verification to make tests pass. Container-only tests cannot
substitute for the actual systemd/OpenSSH/sudo integration.

Required scenarios:

1. Bootstrap twice; provision twice; routes, users, and keys are not duplicated.
2. Deploy a real minimal Docker API; verify loopback readiness and Caddy routing.
3. Deploy a real Linux native binary with the same CLI; verify unit limits,
   startup, environment, logs, restart, and rollback.
4. Deploy a second app; confirm distinct ports, projects, volumes, users, and state.
5. Update an app; confirm the serving commit changes and the previous release
   remains recoverable.
6. Allowed frontend preflight receives the expected CORS permission; an unrelated
   origin does not. Do not mistake missing CORS permission for network blocking.
7. Published app ports are bound to loopback; normal SSH still verifies host keys.
8. Logs and retention stay bounded; cleanup affects only tool-owned resources.

### C. Failure and interruption tests

| Failure | Required behavior |
| --- | --- |
| SSH connection unavailable | Clear failure; existing release unchanged |
| Server host key changed | Refuse connection; no silent trust update |
| Deployment key revoked | Fail authentication; no fallback to other identities |
| Missing sudo permission | Actionable error; no partial privileged mutation |
| Bad image digest, missing image, or registry denial | Fail before stopping old app |
| Wrong binary architecture or checksum | Reject before starting the binary |
| App exits or never becomes ready | Restore preceding release and return nonzero |
| First release fails | Restore prior routing; report no active release |
| Caddy validation/reload failure | Restore previous file and reconcile routing |
| Invalid origins/configuration | Reject before deployment |
| Disk full during staging/state write | Preserve committed state; report failure |
| Concurrent deployments | Serialize; no port/state/config corruption |
| Older queued deployment | Refuse stale promotion unless explicit rollback |
| SSH disconnect during deployment | Job continues; reconnect can read its result |
| Helper killed mid-deploy or interrupted journal found at startup | Journal shows interruption; recovery works |
| Incompatible helper version | Fail before mutation |
| Private-key upload to GitHub fails | Existing working key remains usable |
| Rollback itself fails | Return intervention-required status; never claim success |

Interrupt deployments by killing the worker process and test recovery against the
resulting persisted journal. A real machine reboot is not required for the first
version's test workflow and must not reboot this production server. Document that
actual power-loss/reboot behavior has not been verified unless a separate test
environment is later available.

### D. One real GitHub end-to-end test

After the controlled server installation in section E, use a disposable
application repository and a new test hostname targeting a throwaway app on this
existing server. Push a change from the MacBook. The real hosted runner must
complete checks, publish an image, join Tailscale, authenticate with OpenSSH,
deploy by digest, and verify the public HTTPS response and commit. Repeat with a
deliberately unhealthy release of that throwaway app and confirm rollback plus
a failed workflow result. Verify ephemeral-node cleanup.

This is an explicit integration run, not a test that every pull request runs
against production. Use no paid model calls, existing application data, or
production app identities. Do not replace it with a mocked workflow inspection.

### E. Controlled integration on this server

After GitHub-hosted Ubuntu integration tests pass, install a pinned helper and deploy a
throwaway app under an explicitly selected test hostname. Record baseline
responses for existing routes, verify them after the Caddy integration, and remove
only the temporary app's resources afterward. Keep the helper available for the
first intended real application. Record test evidence and remaining limitations.

## 13. Definition of done

The first version is done only when all of these are true:

- [ ] CLI installs and runs on the user's MacBook without server source paths.
- [ ] Server bootstrap succeeds through normal administrator SSH/sudo access and
      is safely repeatable.
- [ ] CLI/helper protocol and version compatibility are documented and tested.
- [ ] Existing Caddy routes and running applications survive integration.
- [ ] A new ADK application can be generated from the pinned user template.
- [ ] Provisioning sets up a unique local port, route, runtime, persistent data,
      deployment account, and public-key access.
- [ ] Frontend origins reach the existing ALLOW_ORIGINS configuration correctly.
- [ ] Docker deployment uses a tested immutable image built outside the VM.
- [ ] Native Linux binary deployment works through the same user-facing lifecycle.
- [ ] Successful deployments report commit, artifact identity, URL, and readiness.
- [ ] Logs, status, explicit rollback, and interrupted-job recovery work.
- [ ] Failed releases return nonzero and preserve or restore the last good release.
- [ ] CI key creation, repository configuration, and safe key rotation work.
- [ ] A real GitHub push completes the full CI/CD path through Tailscale/OpenSSH.
- [ ] No personal private key is uploaded to GitHub; host trust is pinned.
- [ ] Deployment privileges are scoped to the registered application and helper.
- [ ] Secret values are absent from repository files, normal output, and artifacts.
- [ ] Resource limits, finite timeouts, log limits, and retention are active.
- [ ] Required local, GitHub-hosted Linux, failure, and live workflow tests pass
      with recorded evidence; no user-managed disposable VM is required.
- [ ] README and AGENTS.md let a fresh coding agent perform a release and interpret
      failures without inventing server commands.
- [ ] No additional continuously running deployment service is introduced.

## 14. Implementation sequence and handoff

1. MacBook: create the tooling repository; implement manifest validation, command
   surfaces, packaging, and the versioned protocol. Write boundary tests.
2. MacBook/GitHub-hosted Ubuntu: implement the server helper, forced-command
   dispatcher, and installer. Test SSH/sudo isolation and idempotent bootstrap
   in a runs-on: ubuntu-24.04 integration job.
3. GitHub-hosted Ubuntu: complete Docker deployment, /ready polling, Caddy
   transactions, release state, failure recovery, and bounded retention.
4. MacBook: adapt the ADK template, generate configuration, and wire existing
   quality/build workflows to the shared deploy command.
5. MacBook/GitHub: implement credential setup, Tailscale enrollment, and key
   rotation. Test local boundaries and the generated workflow. Reserve the real
   Tailscale-to-server success/failure test for step 7.
6. GitHub-hosted Ubuntu: add and test the native binary adapter before calling the
   complete two-runtime first version done. The Docker milestone can be used
   earlier if explicitly described as partial scope.
7. This server: install the tested pinned helper, integrate its Caddy import,
   test a throwaway app through the real GitHub/Tailscale/OpenSSH pipeline,
   verify failure recovery, then provision the first intended application.

Do not build or install the privileged helper directly against production as the
primary test environment. Server-specific integration belongs here after the
shared tooling passes the GitHub-hosted Linux tests. No separately owned or
rented disposable VM is a prerequisite.

## 15. Reference sources

- Template: https://github.com/QueryPlanner/google-adk-on-bare-metal
- App factory: https://github.com/QueryPlanner/google-adk-on-bare-metal/blob/main/src/agent/server.py
- Compose: https://github.com/QueryPlanner/google-adk-on-bare-metal/blob/main/compose.yaml
- Existing pipeline: https://github.com/QueryPlanner/google-adk-on-bare-metal/blob/main/.github/workflows/docker-publish.yml
- Caddy imports: https://caddyserver.com/docs/caddyfile/directives/import
- Caddy reload/service: https://caddyserver.com/docs/running
- Tailscale GitHub Action: https://tailscale.com/docs/integrations/github/github-action
- Tailscale federation: https://tailscale.com/docs/features/workload-identity-federation
- GitHub secret setup: https://cli.github.com/manual/gh_secret_set
- GitHub variable setup: https://cli.github.com/manual/gh_variable_set
- OpenSSH authorized keys: https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT
- Docker platform builds: https://docs.docker.com/build/building/multi-platform/
- GitHub-hosted VMs: https://docs.github.com/en/actions/how-tos/manage-runners/github-hosted-runners/use-github-hosted-runners
- Runner administrative access: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- Ubuntu 24.04 runner image: https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md
