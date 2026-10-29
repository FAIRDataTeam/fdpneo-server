# ADR-0028: Version-tagged releases and update notifications

**Status:** Accepted
**Date:** 2026-10-29

## Context

An FDPneo administrator has no way to learn from the application that a newer server or client exists. Operators who track GitHub find out. Everyone else runs whatever they first pulled, including releases with known security fixes.

Three facts make this harder than "compare two version strings":

- **Images are not tagged by version.** CI pushes `ghcr.io/fairdatateam/fdpneo-{server,client}` tagged only by branch (`main`) and commit SHA. Most deployments run the moving `main` tag. "Version 0.19.0 is available" therefore has nothing an operator can pin to.
- **Some releases need manual steps.** 0.18.0 required `FDP_ACCESS_CONTROL_MODE=odrl` on ODRL deployments. 0.19.0 required a PostgreSQL 16→18 migration and a Keycloak realm repair. A notification that just says "update available" invites an upgrade that breaks the deployment.
- **Client and server release independently**, and a client release can require a minimum server version. Client 0.7.0's access-control editor needs server 0.18, for example.

The versions themselves are already known at runtime. The server reports its version from `GET /fdp-api/info`. The client embeds its `package.json` version at build time (`__APP_VERSION__`).

## Decision

### 1. Every release publishes version-tagged images

A `v*` tag in either repository builds and pushes the image with three tags: the full version (`0.19.0`), the minor line (`0.19`) and `latest`. The existing `main` and SHA tags stay. The production `.env.example` defaults to `latest` and tells operators to pin each component to a full version. Server and client versions differ, so the compose files take `SERVER_IMAGE_TAG` and `CLIENT_IMAGE_TAG`, each falling back to `IMAGE_TAG`.

**Amendment (server 0.21.1, client 0.9.2): multi-architecture, signed images.**

- **Native builds per architecture.** Release images are built for `linux/amd64` and `linux/arm64`, each on its own GitHub runner, and merged into one multi-architecture image.
- **Keyless signing.** The image is signed by digest with Sigstore keyless signing. The signing certificate binds the signature to the repository, the `release.yml` workflow and the tag. There are no keys to store or rotate.
- **Transparency log and provenance.** The signature is recorded in Sigstore's public transparency log. A build-provenance attestation (`actions/attest-build-provenance`) records how, and from which commit, the image was built.
- **Verification.** Anyone can verify an image:

  ```bash
  cosign verify ghcr.io/fairdatateam/fdpneo-server:0.21.1 \
    --certificate-identity https://github.com/FAIRDataTeam/fdpneo-server/.github/workflows/release.yml@refs/tags/v0.21.1 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  gh attestation verify oci://ghcr.io/fairdatateam/fdpneo-server:0.21.1 --repo FAIRDataTeam/fdpneo-server
  ```

Images published before these versions are amd64-only and unsigned. The `main` and commit-SHA images built on every merge also stay amd64-only and unsigned, which keeps CI cost down. They are development builds, not releases.

### 2. Every release carries machine-readable upgrade metadata

Each repository keeps a `release.json` at its root, updated in the release commit:

```json
{
  "schema": 1,
  "component": "server",
  "version": "0.19.0",
  "min_upgrade_from": "0.17.0",
  "manual_steps": true,
  "security": false,
  "requires": { "server": ">=0.18.0" },
  "upgrade_notes": "https://github.com/FAIRDataTeam/fdpneo-server/blob/v0.19.0/CHANGELOG.md#0190--2026-10-29"
}
```

- **`manual_steps`** is true whenever the CHANGELOG entry has an *Upgrading* section.
- **`security`** is true when the entry has a *Security* section that fixes a vulnerability. A routine dependency refresh does not count.
- **`requires`** is used by client releases to state the minimum server version.

The release workflow fails if the file's `version` does not match the tag. Otherwise it attaches the file to the GitHub release as `fdpneo-release.json`.

### 3. The server checks for releases, admins see the result

- **A daily check.** A background task asks the GitHub Releases API for the server and client repositories once every 24 hours. It ignores pre-releases and drafts, and reads each release's `fdpneo-release.json`. It sends conditional requests with an ETag, so unchanged results cost nothing against GitHub's unauthenticated rate limit. The result goes into a single-row Postgres table, `release_check`, so replicas share it and a restart does not refetch.
- **Admin endpoint.** `GET /fdp-api/admin/updates` is admin-only. It returns the last check time, the running server version, and the newer server and client releases with their metadata. `POST /fdp-api/admin/updates/check` forces a refresh, at most once a minute.
- **Configuration.**

  | Setting | Default | Meaning |
  |---|---|---|
  | `FDP_UPDATES_CHECK_ENABLED` | `true` | Hard switch. When `false`, the server makes no request, and the runtime setting cannot turn it on. Meant for air-gapped and policy-restricted sites. |
  | `FDP_UPDATES_SERVER_REPO`, `FDP_UPDATES_CLIENT_REPO` | the FAIRDataTeam repositories | Lets forks point at their own releases. |
  | `updates.check` runtime setting | `{"enabled": true}` | Lets an admin switch the check off without a restart. |

- **What leaves the host.** GitHub sees the server's IP address and a `User-Agent` of `fdpneo-server/<version>`. Nothing about the deployment's content, users or identifier base is sent. ADR-0002's metrics boundary is unaffected, since no metrics are involved.

### 4. The client shows a banner to admins only

The client compares the server's running version, from `/fdp-api/info`, and its own `__APP_VERSION__` with the endpoint's answer. For admins, it shows a dismissible banner when either component is behind:

- A **security** release gets a prominent banner that comes back on each session until the deployment is updated.
- A release with **manual steps** says so and links its upgrade notes.
- A client release whose `requires.server` is not met says that the server must be upgraded first.
- Dismissal lasts for the browser session and is held in memory, because the client keeps no browser storage (client CLAUDE.md). A dismissed notice returns after a reload, which suits security releases in particular.

Instance settings gain an *Updates* panel. It shows the current and latest versions, the last check time, the toggle for `updates.check`, and a "check now" button.

## Alternatives considered

- **The browser checks GitHub directly.** Rejected. Every admin's browser would contact GitHub and reveal their IP address. The client's Content-Security-Policy would need a new `connect-src`. Air-gapped sites could not turn it off centrally.
- **An FDPneo-operated update service.** Rejected. It needs infrastructure the project does not run, and it would centralise knowledge of which deployments exist.
- **PyPI's JSON API.** Rejected as the source. It covers only the server and carries none of the upgrade metadata.
- **Parsing CHANGELOG.md instead of a metadata file.** Rejected. Markdown headings are a fragile contract. The metadata file is small, versioned with the release, and validated in CI.

## Consequences

**Easier:**
- Operators learn about releases, and above all security releases, without watching GitHub.
- "Pin to a version" becomes possible and documented, which also makes rollbacks a one-line `.env` change.
- The metadata is the foundation for the optional updater (ADR-0030), which must refuse releases that need manual steps.

**Harder:**
- Every release commit must update `release.json`. CI enforces the version match. Choosing `manual_steps` and `security` correctly stays a reviewer's judgement.
- An outbound request to `api.github.com` by default. Sites that forbid any egress must set `FDP_UPDATES_CHECK_ENABLED=false`. The deployment docs say so prominently.
- Releases published before this ADR have no metadata. The client shows them as "release notes unknown; read the changelog" rather than guessing.
