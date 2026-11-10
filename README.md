# Shiplord

Shiplord sends a tested app to your own Linux server. It connects over SSH, checks that the app works, puts HTTPS in front of it, and restores the previous version if an update fails.

You build the app on your computer or in CI. Shiplord deploys a ready Docker image or Linux program. It does not build the app on the server.

## What you need

- A computer with Python 3.11 or newer, Git, and OpenSSH.
- A Linux server with systemd, SSH, administrator access, and Caddy.
- Docker on the server if you deploy Docker images.
- A DNS record that points your app's domain to the server. Shiplord checks DNS but does not change it.

## Install

```sh
python -m pip install shiplord
shiplord --version
shiplord doctor --local
```

To upgrade, run `python -m pip install --upgrade shiplord`. The GitHub Actions workflow that Shiplord generates installs the same released version.

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
  --frontend https://orders.example.com

# Create this app's deployment access
shiplord provision
```

For an existing Docker app, run `shiplord configure` in its directory and choose the `generic` template. Shiplord will check that the app answers its readiness URL before it sends visitors to it.

For a native Linux program, use the `google-adk` template above. Add `--runtime native` to `shiplord configure` before running `shiplord provision`. Shiplord assumes Docker otherwise.

## Deploy an update

For a Docker app, build an image and push it to a registry. Deploy it by its digest, which identifies the exact image, and by the Git commit that produced it:

```sh
shiplord plan \
  --image ghcr.io/owner/orders@sha256:<image-digest> \
  --commit <40-character-commit-sha>

shiplord deploy \
  --image ghcr.io/owner/orders@sha256:<image-digest> \
  --commit <40-character-commit-sha>
```

`plan` previews the change. It does not deploy. For a native Linux program, build it for the server's CPU and run `shiplord deploy --binary ./dist/orders --commit <commit-sha>`.

If the new version fails its health check, Shiplord restores the last working version. It does not undo changes your app made to its saved data.

## Check or undo a deployment

```sh
shiplord status
shiplord logs --tail 100
shiplord rollback
shiplord recover
```

Use `shiplord env set --from-file .env.production` to set app secrets. Shiplord does not print or log secret values.

## Learn more

- [Full guide](docs/guide.md): commands, options, Docker apps, and GitHub Actions.
- [Operations guide](docs/operations.md): server checks and recovery steps.
- [Protocol](docs/protocol.md): how the client and server communicate.
- [Verification evidence](docs/evidence.md): tests and real-server checks.
- [Design plan](ship-cli-plan.md): project design and acceptance criteria.
