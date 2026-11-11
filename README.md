# Shiplord

Shiplord sends a tested app to your own Linux server. It connects over SSH, checks that the app works, puts HTTPS in front of it, and restores the previous version if an update fails.

You build the app on your computer or in CI. Shiplord deploys a ready Docker image or Linux program. It does not build the app on the server.

## What you need

- A computer with Python 3.11 or newer, Git, and OpenSSH.
- A Linux server running **Ubuntu 24.04 or later, or Debian 12 or later**, with systemd, SSH, administrator
  access, and Caddy from its package repository (`/usr/bin/caddy`). The helper needs **Python 3.11 or later at
  `/usr/bin/python3`**; `shiplord server bootstrap` reads `/etc/os-release` and refuses any other distribution or
  an older version with a message naming what is supported.
- Docker on the server if you deploy Docker images.
- A DNS record that points your app's domain to the server. Shiplord checks DNS but does not change it.

## Install

```sh
python -m pip install shiplord
shiplord --version
shiplord doctor --local
```

`shiplord github setup` pins the same version in the generated workflow (`shiplord==<version>`). When you run
Shiplord from a git checkout, pass `--tooling-ref <commit>` instead.
For a PyPI install, the composite deploy action is pinned separately to commit `465a02fd0d76308546893d2cc37a94254e07c598` in `QueryPlanner/shiplord`, which contains
`deploy/action.yml`; it is not inferred from a PyPI version tag (the `v0.2.0` tag predates the action).
With `--tooling-ref`, both the installed CLI and the action use that commit in the tooling repository.

## Set up once

Replace the example names and domains with your own.

```sh
# Register a server you can already reach over SSH
shiplord server add production --ssh admin@server.example.com

# Prepare the server (requires administrator access)
shiplord server bootstrap production

# Create an app and configure where it will run
shiplord init orders --template google-adk
cd orders
shiplord configure --server production \
  --api-domain orders-api.example.com \
  --frontend https://orders.example.com \
  --image-repository owner/orders

# Create this app's deployment access
shiplord provision
```

`--image-repository owner/orders` is the GitHub repository that publishes the app's image. `provision` reads
`ghcr.io/owner/orders` from `ship.toml`, so it needs no extra flags. In a git checkout you can leave the option
out: `configure` reads the repository from the `origin` remote when it is a GitHub URL, and asks for it when it is
not.

For an existing Docker app, run `shiplord configure` in its directory and choose the `generic` template. It records
the image repository the same way - from the git remote, from `--image-repository owner/repository`, or by asking -
so `shiplord provision` needs no extra flags. Shiplord will check that the app answers its readiness URL before it
sends visitors to it.

For a native Linux program, use the `google-adk` template above without `--image-repository` (a native app has no image) and add `--runtime native` to `shiplord configure` before running `shiplord provision`. Shiplord assumes Docker otherwise.

## Deploy an update

Images can live on GitHub Container Registry (the `[github]` table means `ghcr.io/owner/name`) or Docker Hub
(`[image] repository = "docker.io/namespace/name"`). Deploy either by its digest, which identifies the exact
image, and by the Git commit that produced it:

```sh
shiplord plan \
  --image ghcr.io/owner/orders@sha256:<image-digest> \
  --commit <40-character-commit-sha>

shiplord deploy \
  --image ghcr.io/owner/orders@sha256:<image-digest> \
  --commit <40-character-commit-sha>
```

`plan` previews the change. It does not deploy. For a native Linux program, build it for the server's CPU and run `shiplord deploy --binary ./dist/orders --commit <commit-sha>`. `shiplord github setup --repo owner/name --build-command '<build command>' --binary-path dist/orders` generates a workflow that builds the binary, uploads it as an artifact, and deploys it.

To build and deploy in one step from the application's checkout, run `shiplord release`. It refuses a dirty
working tree, builds and pushes the image for the server's platform with the commit recorded in the image's
`org.opencontainers.image.revision` label, reads the pushed digest, and deploys that digest. Nothing has to be
copied by hand. The platform defaults to `linux/amd64`; pass `--platform linux/arm64` for an arm64 server. Use
`--image-repository` when `ship.toml` has no `[github]` section, and `--tag` to override the default tag (the
commit SHA).

If the new version fails its health check, Shiplord restores the last working version. It does not undo changes your app made to its saved data. For app data, the supported path is one SQLite database on the app's volume (or data directory); see [Application data](docs/guide.md#application-data).

## Check or undo a deployment

```sh
shiplord status
shiplord logs --tail 100
shiplord logs --follow --timeout 300      # Ctrl-C stops it sooner
shiplord rollback
shiplord recover
```

From any directory, `shiplord recover <app> --server <alias>` reconciles a named application without a checkout;
`shiplord status` prints that exact command when it finds unfinished work.

Use `shiplord env set --from-file .env.production` to set app secrets, or `shiplord env set KEY=VALUE` to change
one variable without rewriting the rest. Preview the key names it would add or remove with `shiplord env diff
--from-file .env.production`. Shiplord does not print or log secret values; a value passed on the command line does
appear in your shell history and local process list, so prefer `--from-file` for secrets. Run `shiplord restart` to
apply the current environment to the running release without a new commit.

## Learn more

- [Full guide](docs/guide.md): commands, options, Docker apps, and GitHub Actions.
- [Operations guide](docs/operations.md): server checks and recovery steps.
- [Protocol](docs/protocol.md): how the client and server communicate.
- [Verification evidence](docs/evidence.md): tests and real-server checks.
- [Design plan](ship-cli-plan.md): project design and acceptance criteria.
