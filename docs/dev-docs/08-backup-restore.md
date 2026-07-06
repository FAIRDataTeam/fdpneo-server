# 8. Backup, restore, and migration

Operator runbook for the storage-level backup/restore/migration tooling
([ADR-0016](../adr/0016-backup-restore-migration.md)). All commands are
admin-operated CLI (`fdp backup …`), reading and writing the triple store
directly through the adapter — **not** the LDP API — so provenance
(`dct:created`/`dct:modified`/creator/state), the ADR-0019 record–schema binding
(`dct:conformsTo` / `fdp-o:validatedAgainst`), and the per-record `/audit` graphs
survive a round trip byte-for-byte.

Run these against the same environment the server uses (they read `FDP_*` settings
for the triple store and Postgres). Whole-store only in v1 — no partial/selective
dumps.

## Dump

```bash
fdp backup dump ./backup-2026-07-06
```

Writes a versioned archive:

- **`records.nq`** — every named graph in the store (records + `/meta` + `/audit`
  siblings, plus the reserved profile / schema / resource-definition / policy /
  license graphs and their immutable version snapshots), as N-Quads.
- **`manifest.json`** — dump-format version, `identifier_base`, application
  version, **data-model version** (`adr-0019` vs `legacy`), graph/quad counts,
  per-file SHA-256, timestamp.
- **`audit.jsonl`** — the Postgres `record_audit` rows (skip with `--no-audit`).

## Restore (same base)

```bash
fdp backup restore ./backup-2026-07-06        # into an empty store
fdp backup restore ./backup-2026-07-06 --merge      # skip graphs that already exist
fdp backup restore ./backup-2026-07-06 --overwrite  # replace existing graphs
fdp backup restore ./backup-2026-07-06 --dry-run    # report only
```

Loads the quads **verbatim** (no re-stamped provenance). Preconditions:

- the deployment's `identifier_base` **must equal** the manifest's — a faithful
  restore never re-mints IRIs. On a mismatch it refuses and points you at
  `fdp backup import --rebase`.
- a non-empty store is refused unless `--merge` or `--overwrite`.

After loading, `restore` verifies the `records.nq` checksum, inserts the
`audit.jsonl` rows, **migrates a pre-ADR-0019 dump forward** (backfills
`conformsTo`/`validatedAgainst`, wraps schemas as profiles) when the manifest's
data-model version is `legacy`, and **reindexes search** (see below).

## Migration / adoption

Two `import` modes, for records **not** already under this deployment's base:

```bash
# Adopt an FDPneo dump captured under a DIFFERENT identifier_base:
fdp backup import ./other-fdp-dump --rebase

# Crawl a reference-FDP (e.g. the Java implementation) live over HTTP:
fdp backup import --from https://old-fdp.example.org
```

- **`--rebase`** re-roots every IRI (records, cross-links, and the ADR-0019
  binding — `conformsTo` / `validatedAgainst` / `prof:hasArtifact` and the
  profile/schema graphs) from the dump's base to this deployment's.
- **`--from <url>`** walks the source's LDP tree (breadth-first over
  `ldp:contains`, **egress-pinned to the source origin**), re-roots each record,
  carries the source's `dct:issued`/`dct:modified` into the meta graph, preserves
  the old IRI as a structured alternative identifier (`adms:identifier` +
  `dct:identifier`, ADR-0017 — never `owl:sameAs`), then binds the imported
  records to *this* deployment's profiles and reindexes.

## Admin HTTP API (for the web client)

Besides the CLI, dump and restore are exposed over **admin-only, job-based HTTP
endpoints** (ADR-0016 §5 amendment) so the web client can offer an interactive UI.
They require the `admin` role and drive the same code paths as the CLI.

| Method + path | Purpose |
|---|---|
| `POST /fdp-api/admin/backup/dump` | Start a dump → `202` + a job. Query: `no_audit`. |
| `POST /fdp-api/admin/backup/restore` | Start a restore from an uploaded `.zip` (multipart `archive`) → `202` + a job. Query: `merge`, `overwrite`, `no_audit`, `dry_run`. |
| `GET /fdp-api/admin/backup/jobs/{id}` | Poll job status (`QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`) + the result summary. |
| `GET /fdp-api/admin/backup/jobs/{id}/archive` | Download a finished dump's `.zip`. |

Jobs run in-process and their status is held in memory — **single-worker
deployments** in v1 (a persistent job store is a later scaling step). Restore
uploads are bounded by the global body-size limit (`FDP_BODY_MAX_BYTES`,
default 10 MiB); larger archives use the CLI on the server. `import` (rebase /
reference-FDP crawl) stays CLI-only.

## Two boundaries to know (ADR-0016 §6)

- **Search reindex is part of the runbook.** `metadata_search` is a derived
  projection; `restore` / `import` rebuild it automatically. After a bare
  `fdp pid rebase` (which rewrites the triple store only), run it yourself:

  ```bash
  fdp search reindex
  ```

- **`record_audit` keeps historical IRIs.** A rebase/import rewrites the triple
  store, but the Postgres `record_audit` rows intentionally keep the IRIs that
  were current when the events happened — they are history, not live references.
  Imported `audit.jsonl` rows are appended (their ids autoincrement).

## Privileged provenance

`restore`/`import` write meta graphs with **supplied** timestamps/creator/state
(`MetadataRepository.write_imported`). This capability is CLI-only and never
exposed on the HTTP surface, so the LDP contract's "server-stamped provenance
always" guarantee (ADR-0014) stays un-gameable by API clients (ADR-0016 §5).
