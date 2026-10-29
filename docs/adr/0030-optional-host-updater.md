# ADR-0030: Optional, pull-based host updater

**Status:** Accepted
**Date:** 2026-10-29
**Depends on:** [ADR-0028](0028-release-tags-and-update-notifications.md), [ADR-0029](0029-full-deployment-backup.md)

## Context

ADR-0028 tells admins that a release is available. Applying it on a bundled stack is still a shell session on the host: pin the version, pull, restart, and check readiness. Some operators would like a button.

A container cannot replace itself. Replacing containers means controlling the container runtime, and on a standard Docker host the Docker socket is root-equivalent: anyone who can start containers can mount the host's filesystem. So does membership in the `docker` group. Many institutions will not grant that to an application, and they should not have to. Rootless Docker and Podman reduce the blast radius to one unprivileged user's containers. On Kubernetes, updates belong to the cluster's own tooling, such as Helm or GitOps.

Releases are also not all safe to apply unattended. ADR-0028's `manual_steps` flag exists because 0.18.0 and 0.19.0 both needed operator action.

## Decision

### 1. Off by default, and a separate component

The updater is a separate service, `updater`, in a compose profile of the same name. It does not run unless the operator starts it with `--profile updater`. A systemd unit running the same agent is the alternative packaging for operators who do not want a privileged container. The documentation states the privilege plainly:

- **Docker:** root-equivalent.
- **Rootless Docker or Podman:** the service user's containers only.
- **Kubernetes:** not supported. Use your deployment tooling.

The notification feature of ADR-0028 is complete without it.

### 2. The server never touches the container runtime

The design is pull-based. The server only records intent:

1. An admin clicks **Update** in the client. The server validates the request against the cached release metadata and stores it in a new `update_request` table, together with the requesting subject and a status.
   - The target must be newer than the running version.
   - `manual_steps` must be false.
   - `min_upgrade_from` must be satisfied.
   - A client target's `requires.server` must hold.
2. The updater polls `POST /fdp-api/admin/updates/agent/next` about once a minute. The poll doubles as a heartbeat, and the client offers updates only while an agent was seen within the last five minutes.
   - **Updater-scoped key.** The agent authenticates with an updater-scoped API key, which only an admin can mint. The key carries only the `updater` role, and it stops working when its owner loses the admin role.
   - **Confinement.** The authentication middleware refuses this key everywhere except `/fdp-api/admin/updates/agent/*`. It therefore cannot reach subject-scoped endpoints, such as minting a full key for its owner.
3. The updater **re-validates independently**. It fetches the release and its `fdpneo-release.json` from GitHub itself, and only accepts images from the configured registry and repository. A compromised server can at most request an official, non-manual release. It cannot make the updater run an arbitrary image.

### 3. What the updater does

For a claimed request, the updater runs these steps:

1. Re-validates the release against GitHub, as described in §2.
2. Pulls the target image by tag and pins the digest it received, writing `X.Y.Z@sha256:…`. Every later restart then runs exactly the image that was checked. Signature verification with Sigstore follows once release images are signed.
3. Takes a full backup (ADR-0029) by running the operator's backup command, which prints the archive's location. If the backup fails, the update is abandoned before anything changes.
4. Writes `SERVER_IMAGE_TAG` or `CLIENT_IMAGE_TAG` into the stack's `.env` and recreates only that service. It runs `docker compose` with the stack directory as project directory, so compose override files and `COMPOSE_FILE` apply exactly as they do for the operator.
5. Waits for `/fdp-api/readyz` and for `/fdp-api/info` to report the target version, with a configurable timeout of five minutes by default.
6. Reports success or failure back to the server, retrying while the server restarts. The client shows the outcome in the *Updates* panel.

On failure:

- **No database migration applied.** If the Alembic revision did not change, the updater restores the previous `IMAGE_TAG`, restarts, and reports the rollback.
- **A migration ran.** The updater does not roll back the image, because the old server cannot run on the migrated schema. It reports *manual intervention required*, with the backup's location. Restoring from that backup is a deliberate operator action.

### 4. What the updater never does

- Edit compose files or change PostgreSQL, Keycloak, GraphDB or Caddy versions. Those are stack upgrades with their own migration steps.
- Apply a release with `manual_steps`, or one below `min_upgrade_from`.
- Act without an admin's request. Unattended auto-apply is out of scope for v1. If added later, it would be limited to releases flagged `security` with no manual steps, and enabled per deployment.

## Alternatives considered

- **Mount the Docker socket into the server.** Rejected. It would turn any server compromise, or any compromised admin account, into host root.
- **A generic auto-updater such as Watchtower.** Rejected. It updates whatever tag moves, takes no backup, knows nothing about migrations or manual steps, and needs the same socket access.
- **The updater accepts commands pushed by the server.** Rejected in favour of pull. With pull, the updater needs no listening port, and it trusts the server only for *which* allowed release to apply.

## Consequences

**Easier:**
- Routine releases become a reviewed one-click action with a backup and a defined rollback, for operators who accept the privilege.
- The audit trail records who requested each update and the outcome.

**Harder:**
- A privileged component to build, document and keep secure. It gets its own security review before it ships.
- Stack-level upgrades remain manual. The *Updates* panel says so when a release is skipped for that reason.
- Multi-host or replicated deployments are out of scope for v1, because the updater manages one compose project.
