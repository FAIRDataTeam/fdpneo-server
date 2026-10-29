# Host updater agent (optional)

The host updater applies server and web-client updates that an administrator
requests in the client, under **Instance settings → Updates**. It is
described in [ADR-0030](../../docs/adr/0030-optional-host-updater.md). It is
**off by default**. Update notifications work without it; without it, you
apply updates by hand.

## What it needs, and why that matters

To replace containers, the agent controls the Docker daemon. On a standard
Docker host, that is **root-equivalent**: anyone who can start containers can
mount the host's filesystem. This applies equally to the Docker socket and to
membership of the `docker` group.

| Host | What the agent can do |
|---|---|
| Docker, rootful | Anything root can do on the host. |
| Rootless Docker or Podman | Control the service user's own containers only. |
| Kubernetes | Not supported. Use your deployment tooling, such as Helm or GitOps. |

Only enable it if your security policy accepts that privilege.

## What it does

The agent polls the server about once a minute with a restricted API key. The
poll doubles as a heartbeat, and the *Update* buttons only appear while the
agent is connected. For each request, it runs these steps:

1. **Re-checks the release on GitHub itself.**
   - It must be published and not a pre-release.
   - It must carry `fdpneo-release.json`, with `manual_steps: false`.
   - The running version must meet `min_upgrade_from`.
   - A client release's `requires.server` must hold against the running server.

   The agent trusts the server only for *which* allowed release to apply. It
   never runs an arbitrary image.
2. **Pulls the image by tag** and pins the digest it received, as
   `X.Y.Z@sha256:…`. It then **verifies the image's signature** with
   `cosign`. The image must have been signed by the release workflow of
   FDPneo's GitHub repository for exactly that tag. An unsigned or wrongly
   signed image is refused before anything changes. Releases before server
   0.21.0 and client 0.9.1 are not signed, so apply those by hand.
3. **Takes a full encrypted backup** with the stack's `backup.sh`. If the
   backup fails, nothing changes.
4. **Applies the update.** It writes `SERVER_IMAGE_TAG` or `CLIENT_IMAGE_TAG`
   in the stack's `.env`, then recreates only that service.
5. **Waits for readiness.** The server must report the new version on
   `/fdp-api/info`.
6. **Reports the outcome to the server.** If the new version does not become
   ready:
   - **No migration ran.** The agent restores the previous tag and reports
     *rolled back*.
   - **A migration ran.** The old server cannot run on the new schema, so the
     agent reports *needs attention*, with the backup's location. Restoring
     that backup is your decision.

**What it never does:**
- Edit compose files.
- Touch PostgreSQL, Keycloak, GraphDB or Caddy.
- Apply a release that needs manual steps.
- Act without an administrator's request.

## Enable it

1. **Mint an updater token.** As an administrator, open **API keys** in the
   client, tick **Updater token**, and generate it. The token carries only the
   `updater` role. The server refuses it everywhere except the agent
   endpoints, and it stops working if you lose the admin role.
2. **Prepare the pre-update backup.** Before every update, the agent runs
   the stack's `backup.sh` (ADR-0029). That script writes an encrypted
   bundle with the full application state, the Keycloak realm with its
   users, and `.env`. The agent chooses the archive path itself, under
   `backups/` in the checkout, and keeps the newest three. It needs the
   bundle's passphrase in a file it can read:

   ```bash
   install -m 0600 /dev/null /srv/fdpneo/.backup.pass
   $EDITOR /srv/fdpneo/.backup.pass   # one line: the passphrase
   ```

   Keep this passphrase somewhere safe as well. Without it, the backups
   cannot be restored.
3. **Add the settings to the stack's `.env`:**

   ```bash
   FDPNEO_ROOT=/srv/fdpneo                       # absolute path of this checkout
   FDP_UPDATER_KEY=fdpk_…                        # the updater token
   UPDATER_BACKUP_PASSPHRASE_FILE=/srv/fdpneo/.backup.pass
   # Optional:
   # UPDATER_BACKUP_DIR=/srv/fdpneo/backups      # must lie under FDPNEO_ROOT
   # UPDATER_BACKUP_KEEP=3
   # DOCKER_SOCKET=/run/user/1000/docker.sock    # rootless Docker
   # UPDATER_POLL_SECONDS=60
   # UPDATER_READY_TIMEOUT=300
   # UPDATER_SKIP_BACKUP=true                    # not recommended
   # UPDATER_VERIFY_SIGNATURES=false             # only for unsigned mirrors
   ```

   A different backup command (`UPDATER_BACKUP_COMMAND`) must accept the
   archive path as its last argument, exit non-zero on failure, and create
   that file. Releases are pinned per component with `SERVER_IMAGE_TAG` and
   `CLIENT_IMAGE_TAG`. The agent manages these two lines from now on.
4. **Start the agent with its profile:**

   ```bash
   docker compose --profile updater up -d --build --no-deps updater
   ```

   Keep `--no-deps`. Without it, `--build` also rebuilds every dependency
   that has a `build:` section. In the evaluation stack that includes the
   server and client, which would replace the release images you run with
   local builds.

   The checkout is mounted into the agent container at its host path, so
   `docker compose` inside the container resolves exactly the same files,
   including `compose.override.yaml`, as you do on the host.

### As a systemd service instead

Use `fdpneo-updater.service` if you prefer not to run a privileged container.
The unit file explains the installation. The agent is a single
standard-library Python script, so the host needs Python 3.11 or later, the
Docker CLI with the Compose plugin, and `cosign` for the signature check.

## Disable it

Run `docker compose stop updater`, or start the stack without
`--profile updater`. Then revoke the updater token under **API keys**.
