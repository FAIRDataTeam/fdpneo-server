# ADR-0029: Full-deployment backup for moving and restoring an instance

**Status:** Accepted
**Date:** 2026-10-29
**Extends:** [ADR-0016](0016-backup-restore-migration.md)

## Context

ADR-0016's backup is faithful for the knowledge graph. `fdp backup dump` writes every named graph as N-Quads plus the `record_audit` rows. Moving a deployment to another host and getting back *the same state* needs more than that, and today the operator has to assemble the rest by hand:

| State | Where it lives | In today's dump |
|---|---|---|
| Records, schemas, profiles, policies, licenses, meta and audit graphs | Triple store | Yes |
| Record audit trail | Postgres `record_audit` | Yes |
| Runtime settings, including the access-control mode (ADR-0027) | Postgres `runtime_settings` | No |
| API keys, hashed, with owner subject and roles | Postgres `api_keys` | No |
| Saved searches, FDP index targets, applied profiles | Postgres `search_saved_queries`, `index_targets`, `profile_applied` | No |
| Last IdP-asserted roles per subject | Postgres `subject_principal` | No |
| Metrics history | Postgres `metrics_hourly`, `metrics_daily` | No |
| Users, credentials, clients and realm settings of the bundled Keycloak | Keycloak's database | No |
| Deployment configuration and secrets | The stack's `.env` and any compose override | No |

Three properties constrain the design:

- **Identity is external.** FDPneo has no user table. API keys, ownership and roles reference the IdP's subject identifier (`sub`). A restore that recreates users with new IDs silently orphans every API key.
- **Some state must not travel.** Raw metrics rows carry the pseudonymous visitor hash, and ADR-0002 keeps them short-lived. The authorization cache, search index and external-label cache are derived and are rebuilt anyway.
- **A full backup is a credential store.** Keycloak password hashes, client secrets and `.env` secrets make the archive as sensitive as the host itself.

## Decision

### 1. Two layers

- **The application archive** is produced by the server. It contains everything FDPneo owns and is independent of the IdP and the hosting. It is ADR-0016's archive with new members, dump format version `2`.
- **The stack bundle** is produced by a script in `deploy/stack`, like the PostgreSQL upgrade script. It wraps the application archive together with the bundled Keycloak's export and the deployment configuration. It is what an operator uses to move a whole bundled stack.

Deployments with an external IdP use the application archive alone. Their IdP is backed up by whoever runs it.

### 2. The application archive gains the Postgres state

`fdp backup dump --full` adds one JSON-lines file per state table under `postgres/`:

- **Included:** `runtime_settings`, `api_keys`, `search_saved_queries`, `index_targets`, `profile_applied`, `subject_principal`, `metrics_hourly`, `metrics_daily`, and `record_audit`, which is already included today.
- **Excluded, rebuilt on restore:** `authz_index`, `metadata_search`, `metadata_external_labels`.
- **Excluded by design:** `metrics_raw`. The dump runs the metrics rollup first, so no completed hour is lost.

The manifest records the Alembic revision of the source database and a row count and SHA-256 per file. Without `--full`, the dump is exactly today's archive.

### 3. Restore migrates through the source's schema

`fdp backup restore` of a full archive requires an empty Postgres for the state tables, unless `--overwrite` is given. It then does the following:

1. Refuses an archive whose Alembic revision this server does not know. That means a dump from a newer server, which an older server cannot read.
2. Migrates the empty database to the archive's revision, loads the rows, then migrates to the current head. Rows are therefore always loaded into the schema they were written for, and existing migrations carry them forward.
3. Restores the triple store as today, then rebuilds the search index and lets the authorization cache refill on demand.

A restore onto a different domain composes with the existing `--rebase`. That rewrites the identifier base in the triple store. `api_keys`, `subject_principal` and settings carry no record IRIs, and `record_audit` keeps historical IRIs as ADR-0016 §6 already states.

### 4. The stack bundle carries identity and configuration

`deploy/stack/backup.sh` and `deploy/stack/restore.sh`, run from the stack directory:

- **`backup.sh`**
  1. Stops the server, so nothing writes during the dump, and runs `fdp backup dump --full` in a one-off server container.
  2. Exports the Keycloak realm *with users* using Keycloak's own `kc.sh export --users realm_file` in a one-off container. The export keeps user IDs, so `sub` values, and with them API keys and ownership, stay valid after the move.
  3. Copies `.env` and any `compose.override.yaml`, then restarts the stack.
  4. Writes one encrypted archive, with a manifest listing what it contains and the image versions it was taken from.
- **`restore.sh`** runs on the new host with the same compose files.
  1. Checks the image versions.
  2. Restores `.env`, and lets the operator change `PUBLIC_HOST` before anything starts.
  3. Imports the realm, restores the application archive, and starts the stack.

Certificates are not included, because Caddy re-obtains them. GraphDB's own repository configuration comes from the compose files.

### 5. Credentials are encrypted at rest

Any archive that contains credentials is encrypted with a passphrase. That covers full application archives, because they hold API key hashes, and every stack bundle.

- **Scheme.** The archive uses scrypt for key derivation and AES-256-GCM in fixed-size chunks, with a versioned header (`FDPNEO-ENC1`, salt, nonce prefix). It uses the `cryptography` package the server already depends on, so no new runtime dependency is added.
- **Commands.** `fdp backup seal` and `fdp backup unseal` expose the format. The stack scripts call them inside the server image, so the host needs no extra tools.
- **Defaults.** On the command line, full dumps are encrypted by default, and `--no-encrypt` must be explicit. The passphrase is read from a prompt or from a file, never from a command-line argument.
- **Verification.** `fdp backup verify <archive>` checks the passphrase, the checksums and the manifest without restoring.

### 6. The admin HTTP API offers full backups only encrypted

`POST /fdp-api/admin/backup/dump?scope=full` requires a passphrase in the request body, and the archive it produces is always encrypted. The passphrase is never logged or persisted. During an HTTP-triggered full dump, the server briefly refuses writes with `503` and `Retry-After`, so the dump is consistent. Reads continue. Restoring a full archive over HTTP stays subject to the body-size limit. Larger archives use the command line, as today.

## Alternatives considered

- **Volume snapshots or `pg_dumpall` plus a GraphDB data-directory copy.** Simple, but tied to the exact database and store versions on both sides. Today's PostgreSQL 16→18 move is the counter-example. They also capture derived state and raw metrics. They remain a valid operator tool, but they are not the supported migration path.
- **The server exports Keycloak through its admin API.** Rejected. The server's service account deliberately has user-management rights only, and a realm export with credentials needs master-realm administration. Keeping it in the stack script keeps that privilege off the server.
- **Unencrypted archives with "store them securely" guidance.** Rejected for anything holding credentials. Backups get copied to laptops and shared drives.

## Consequences

**Easier:**
- Moving a bundled deployment to a new host is two commands, and the result has the same records, settings, users, API keys and metrics history.
- The archive works across server versions and triple stores, because it is logical and migration-aware. That also makes it the route for a store change, for example to GraphDB 11 or a license-free store.
- The optional updater (ADR-0030) gets a complete pre-update backup.

**Harder:**
- Each new Postgres table needs a classification: state, derived, or excluded. A unit test fails when a table is missing from the classification, so it cannot be forgotten.
- Losing the passphrase means losing the backup. The tooling says so at dump time.
- `backup.sh` stops the server for the duration of the dump. That is minutes for typical deployments, and the HTTP path avoids the full stop by refusing writes instead.

## Implementation notes

These refinements emerged while building and testing the decision. None of them changes it.

- **Evaluation-stack Keycloak volume.** In dev mode, Keycloak keeps its H2 database inside the container and locks it while running, so no one-off container could export it. The evaluation stack now mounts a `keycloak-data` volume. Both stacks then use the same procedure: stop Keycloak, export with a one-off container, start it again.
- **HTTP restore and schema revisions.** A full restore over HTTP refuses an archive whose schema revision differs from the database's, because the schema must not move under a running server. The CLI, with the server stopped, migrates freely.
- **`--merge` and Postgres state.** `--merge` is refused for full archives, because Postgres state cannot be merged row by row. `--overwrite` replaces it instead.
- **Bundle layout.** The stack bundle is an encrypted tar holding `bundle.json`, the unencrypted application archive `app.zip`, the Keycloak export and the configuration. The outer encryption authenticates all of it.
- **Domain changes.** When the identifier base changes, `restore.sh` uses `fdp backup import --rebase`, rewrites the old host in the exported realm's URLs, and re-keys metrics with `fdp metrics rebase-resources`.
- **Master realm.** Only the FDP realm is exported. Keycloak's master-realm bootstrap admin is recreated from the deployment's environment on the new host.

