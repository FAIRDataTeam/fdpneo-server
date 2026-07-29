# Changelog

All notable changes to the FDPneo server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning while pre-1.0 (minor versions may carry breaking API changes, called
out explicitly below).

## [0.13.1] — 2026-07-29

### Fixed

- **Importing `fdpneo_server.main` no longer builds an application.** The
  module-level `app` was constructed at import time, so a bare import
  demanded full configuration (`FDP_TRIPLESTORE_*`, `POSTGRES_DSN`) and
  leaked one un-closed `httpx.AsyncClient` per process — even for embedders
  about to call `create_app(triple_store_factory=…)` with their own storage,
  and for tools that import the module only to reach a symbol. `app` is now
  built lazily on first attribute access (PEP 562) and cached;
  `uvicorn fdpneo_server.main:app` and `fastapi dev` are unaffected.
  Import-time behaviour change only — no configured deployment changes.

## [0.13.0] — 2026-07-29

Downstream composition seams ([ADR-0023](docs/adr/0023-downstream-composition-seams.md)):
`create_app()` gains keyword-only, optional parameters so platforms that extend
the server by composition stop monkeypatching internals. No behavior change for
callers that pass nothing.

### Added

- **`create_app(triple_store_factory=...)`** — replaces the internal
  `TripleStoreAdapter.from_settings` call. The factory receives the resolved
  `TripleStoreSettings` and returns the adapter instance every service uses,
  so a downstream can mediate all RDF I/O (driver quirks, telemetry, query
  budgets) by subclassing or wrapping the adapter. Public type alias:
  `fdpneo_server.main.TripleStoreFactory`.
- **`create_app(extension_routers=[...])`** — routers mounted after every
  reserved `/fdp-api` router and immediately before the LDP catch-all: they
  win the paths they claim, cannot shadow the FDP's fixed API, and everything
  else falls through to LDP unchanged.

### Notes

- Both parameters are public API under semver; everything on `app.state`
  remains internal. Extracting a structural `TripleStorePort` protocol is
  deliberately deferred (see the ADR).

## [0.12.0] — 2026-07-29

Packaging and naming release: the wheel is now fully self-contained (an
installed FDP can migrate its database and bootstrap itself with no source
checkout), and the import package no longer squats the generic `fdp` name.

### Breaking

- **Import package renamed `fdp` → `fdpneo_server`.** The distribution stays
  `fdpneo` and the CLI command stays `fdp`; only Python imports change.
  Migration: `import fdp` → `import fdpneo_server`, `from fdp.X import Y` →
  `from fdpneo_server.X import Y`. Rationale: `fdp` implied the whole stack
  (server + client) and collided with downstream distributions that provide a
  `fdp_server` module; `fdpneo_server` is unambiguous and matches the
  distribution name. Error codes (`fdp.not_found`, …), the `/fdp-api` prefix,
  the `fdp` CLI, and all wire formats are unchanged.

### Added

- **The default DCAT profile bundle ships in the wheel** at
  `fdpneo_server/profiles/default/`. `fdp profile apply` and
  `fdp profile validate` default to the bundled profile when no path is given,
  and the lifespan auto-bootstrap (`FDP_PROFILE_AUTO_APPLY=true`) falls back to
  it when `FDP_PROFILE_PATH` is unset — so an installed FDP can bootstrap
  itself. New helper: `fdpneo_server.metadata.profiles.bundled_default_profile()`.

### Fixed

- **`fdpneo_server.__version__` reports the real version** — it queried the
  pre-rename distribution name (`fdp`) after the PyPI distribution became
  `fdpneo` (0.11.1), so it always fell back to `0.0.0`.
- **`fdp db migrate` now works from an installed wheel.** `migrations/` and
  `alembic.ini` moved into the package (`src/fdpneo_server/`) and ship as package data;
  the CLI and tests resolve them with `importlib.resources` instead of
  `Path(__file__).parents[2]`, and `alembic.ini` points at the packaged scripts
  via `script_location = fdpneo_server:migrations`. Downstream deployers no longer need to
  vendor the migration tree next to the venv, and the Dockerfile no longer copies
  `alembic.ini`/`migrations/` separately. Dev note: create revisions with
  `uv run alembic -c src/fdpneo_server/alembic.ini revision --autogenerate -m "..."`.

### Client coordination

- Downstreams that import the server (composition or extension) must update
  imports to `fdpneo_server` and require `fdpneo>=0.12.0`. Deployment vendoring
  workarounds for `migrations/`/`alembic.ini`/`profiles/` can be deleted.

## [0.11.0] — 2026-07-09

In-band affordance advertisement / HATEOAS completion
([ADR-0022](docs/adr/0022-in-band-affordance-advertisement.md)): a generic RDF/LDP
client can now reach every management view, page a container, and discover the API
description by following links — no URL-template convention or OpenAPI required.
Everything stays RDF + RFC 8288 Web Linking (no HAL/Hydra).

### Added

- **RFC 8288 pagination links** on `GET /{urlPrefix}/page/{childPrefix}`:
  `Link: rel="first"/"prev"/"next"/"last"`, computed from `offset`/`limit`/`total`
  (preserving the caller's other query params).
- **Management-affordance `Link` relations** on record `GET`/`HEAD`, under the
  provisional FDP-O extension namespace `https://w3id.org/fdp/o#` (pending FDP-O WG
  harmonization; opaque extension rels, so a later IRI swap is compatible):
  - `hasMetaMetadata` → `<record>/meta`
  - `hasSpec` → `<record>/spec` (instance) and `<base>/<urlPrefix>/spec` (type)
  - `hasExpandedView` → `<record>/expanded`
  - `hasStateTransition` → `<record>/state`
  - `hasMemberPage` → `<record>/page/{childPrefix}` (per child type, containers)

  Advertised unconditionally (the PDP still authorizes each request); *fixed*
  relations for the `MAX_LINKS` cap (only surplus `item` links are trimmed).
- **Allowed next states** on the served `<record>/meta` representation:
  read-time `fdp-o:allowedStateTransition` view triples derived from the lifecycle
  state machine — **never persisted, never dumped, never SHACL-validated**.
- **Self-describing resource-definition catalog**: `ResourceDefinitionView` gains
  an absolute `links` object (`self`, `container`, `spec`) built from the serving
  base. `urlPrefix` stays for compatibility.
- **Root API-description links**: the root FDP record advertises
  `service-desc` (`/fdp-api/openapi.json`), `service-doc` (`/fdp-api/docs`, when
  the docs UIs are enabled), and `hasResourceDefinitions`
  (`/fdp-api/resource-definitions`).

### Deprecated

- The `X-FDP-Page-Total/Offset/Limit` headers on `/page` are deprecated in favour
  of the RFC 8288 navigation links; **removal targeted v0.12.0**.

### Client coordination

- `fdp-client`: regenerate API types for `ResourceDefinitionView.links`, migrate
  paging to `rel="next"` traversal, and drop `X-FDP-Page-*` reads.
- `fdp-mcp` (ADR-0018): can replace its URL-template knowledge for
  `/spec`, `/meta`, and `/state` with link-following.

## [0.10.0] — 2026-07-07

External (remote) label resolution for `GET /fdp-api/labels` (ADR-0012 extension):
the endpoint can now dereference external identifiers to human labels, off by
default and allow-listed.

### Added

- **Third label source** on `/fdp-api/labels`: when an IRI has no local label,
  the resolver dereferences it over content-negotiated RDF (Turtle / RDF-XML /
  JSON-LD), extracts a label (`rdfs:label` › `skos:prefLabel` › `dct:title` ›
  `foaf:name` › `schema:name`, scored by language then predicate), and caches it.
  Resolves DOIs, ORCIDs, and SKOS/linked-data vocabularies. **ROR is JSON-only
  and not resolved by this generic RDF path** (a per-source adapter is deferred).
- **`?wait=<ms>` query param** on `/labels`: external resolution is **lazy by
  default** (a first-seen IRI is fetched in the background and returned on a later
  call); `?wait` opts into a bounded inline wait (capped by `max_wait_ms`).
- **Persistent cache** — a Postgres table `metadata_external_labels` (migration
  `0009`, keyed by `(iri, language)`, negatives cached) fronted by the existing
  in-memory TTL cache. Resolved labels survive restarts and are shared across
  workers.
- **`FDP_REMOTE_LABELS_*` settings** (`RemoteLabelSettings`): `enabled` +
  `allowed_hosts` (empty denies all), fetch bounds (`timeout_seconds`,
  `max_bytes`, `max_redirects`, `max_concurrent_fetches`), TTLs, and `max_wait_ms`.
- The `sdo` (schema.org) namespace prefix.

### Security

- Off by default; runs only with the switch on **and** a non-empty host
  allow-list. Every fetch is allow-listed and SSRF-guarded on each redirect hop
  (`shared.ssrf.assert_public_url`), size- and time-capped, and parsed through
  `shared.negotiation` so the JSON-LD remote-`@context` block (audit F-01/R-01)
  is preserved.

### Notes

- Same outbound posture as remote schema sync; recorded as the "remote-vocabulary
  labels" amendment to [ADR-0012](docs/adr/0012-first-class-odrl-policy-and-license-documents.md).
- No API change when disabled — `/labels` behaves exactly as before.

## [0.9.0] — 2026-07-06

Admin-only HTTP endpoints for backup/restore (ADR-0016 §5 amendment), so the web
client can offer an interactive backup/restore UI.

### Added

- **Admin backup/restore API** under `/fdp-api/admin/backup` (requires the `admin`
  role), job-based:
  - `POST /admin/backup/dump` → `202` + a job; dumps the store to a downloadable
    archive.
  - `POST /admin/backup/restore` → `202` + a job; faithfully restores an uploaded
    dump archive (`merge` / `overwrite` / `no_audit` / `dry_run`).
  - `GET /admin/backup/jobs/{id}` → poll job status + result summary.
  - `GET /admin/backup/jobs/{id}/archive` → download a finished dump's `.zip`.

  The endpoints drive the same `dump_store` / `restore_store` code paths as
  `fdp backup …` — the HTTP layer is only a role-gated trigger, so the
  server-stamped-provenance guarantee (ADR-0014) is unchanged for ordinary API
  clients. Jobs run in-process with in-memory status (single-worker in v1); restore
  uploads are bounded by the body-size limit (larger archives use the CLI). `import`
  stays CLI-only. Runbook: [dev-docs/08-backup-restore.md](docs/dev-docs/08-backup-restore.md).

### Changed

- Factored the dump/restore workflows into `metadata/backup/orchestrate.py`
  (`dump_to_archive`, `orchestrate_restore`) shared by the CLI and the endpoints.

## [0.8.0] — 2026-07-06

Outbound Index ping (Phase 8.1 / ADR-0020/0021): a FDPneo deployment now announces
itself to FDP Index instances so they harvest it and keep it discoverable.

### Added

- **Outbound Index ping.** When `FDP_INDEX_PING_TARGETS` is set, the server POSTs
  `{"clientUrl": <base>}` to each configured index — the reference wire protocol
  (`POST {index}/`, `204` accepted, `429` rate-limited), verified against the
  reference `IndexPingController`. It pings on startup (deployment announce), every
  `FDP_INDEX_PING_INTERVAL_SECONDS` (default 7 days), and on record changes
  (throttled by `FDP_INDEX_PING_MIN_INTERVAL_SECONDS` to respect the index's rate
  limit). Per-target failures are logged, never fatal.
- **`fdp index ping`** — one-shot CLI for driving pings from an external scheduler
  (`FDP_INDEX_PING_IN_PROCESS=false` disables the in-process loop).
- Settings: `FDP_INDEX_PING_TARGETS` (comma-separated index base URLs, empty ⇒
  disabled), `_INTERVAL_SECONDS`, `_ON_PUBLISH`, `_MIN_INTERVAL_SECONDS`,
  `_TIMEOUT_SECONDS`, `_IN_PROCESS`, `_CLIENT_URL`.

Only the outbound side ships here; the Index intake/harvest is a separate product
(FAIR Discovery, ADR-0021).

## [0.7.0] — 2026-07-06

Faithful backup / restore / migration (Phase 18 / ADR-0016 §2–§6). Storage-level,
admin-operated CLI tooling that round-trips a deployment byte-for-byte and adopts
records from other instances — provenance, the ADR-0019 record–schema binding, and
audit graphs all preserved.

### Added

- **`fdp backup dump <dir>`** — export every named graph as N-Quads plus a
  versioned `manifest.json` (dump-format + data-model version, `identifier_base`,
  graph/quad counts, per-file SHA-256) and an optional `audit.jsonl` of the
  Postgres `record_audit` rows. Reads through the adapter; the LDP layer is not
  involved. Blank-node labels are re-minted per graph so N-Quads' document-scoped
  blank nodes can't conflate records on restore.
- **`fdp backup restore <dir>`** — faithful, verbatim load (no re-stamped
  provenance). Verifies the checksum; refuses an `identifier_base` mismatch (points
  at `import --rebase`) or a non-empty store (unless `--merge`/`--overwrite`);
  `--dry-run`. Afterwards inserts audit rows, migrates a pre-ADR-0019 dump forward,
  and reindexes search.
- **`fdp backup import --rebase <dir>`** — adopt an FDPneo dump captured under a
  different base, re-rooting every IRI (including the ADR-0019 cross-references) via
  the shared `pid/rebase` rewrite.
- **`fdp backup import --from <url>`** — migrate a reference-FDP instance by
  crawling its LDP tree (egress-pinned to the source origin): re-root each record,
  carry source `dct:issued`/`dct:modified` into the meta graph, preserve the old
  IRI as `adms:identifier` (ADR-0017), then bind to this deployment's profiles.
- **Privileged provenance write path** (`MetadataRepository.write_imported`) —
  writes meta graphs with supplied created/modified/creator/state; CLI-only, never
  on the HTTP surface (ADR-0016 §5), so ADR-0014's server-stamped-provenance
  guarantee stays un-gameable.
- Operator runbook: [dev-docs/08-backup-restore.md](docs/dev-docs/08-backup-restore.md).

### Changed

- `pid/rebase` `rebased` / `rewrite_graph` and `identifiers.record_alternative_identifier`
  promoted to public (the ADR-0016 shared rewrite + alt-id helpers).
- Search reindex factored into `metadata/search/reindex.py` (`reindex_all`), shared
  by `fdp search reindex` and the restore/import flows.

## [0.6.0] — 2026-07-05

Agent consumption: server-side support for the `fdp-mcp` bridge (Phase 19 /
ADR-0018). The full-stack deployment now comes up agent-ready, and the FDP
advertises its query endpoints so agents can discover them.

### Added

- **Endpoint discovery in the root metadata (gap G-05).** The root FDP record now
  advertises this deployment's query services — `void:sparqlEndpoint` plus DCAT
  `dcat:DataService` descriptors (`dcat:endpointURL` / `dcat:endpointDescription`)
  for the SPARQL endpoint and, when enabled, the search API — so a client (the
  `fdp-mcp` bridge, or any consumer) discovers them from the root instead of being
  hand-configured with endpoint paths. Added the VOID namespace (`void`).
- **`fdp-mcp` in the full-stack deploy profile.** `deploy/stack/compose.yaml` gains
  an `mcp` service (streamable-HTTP MCP at `:8002/mcp`, liveness at `/healthz`),
  wired to the server so a default deployment is agent-ready. Off switch:
  `--scale mcp=0`. Read-only, public-surface-only (ADR-0018).
- **Agent-ready conformance note** (`docs/conformance/agent-ready.md`): which
  MCP tool-surface tools this server backs (all 5 required + 3 optional) and the
  gap triage.

### Changed

- **Root advertisement self-heals on restart.** A deployment bootstrapped before
  G-05 gains the advertisement idempotently on the next startup
  (`ensure_root_service_advertisement`) — no destructive re-apply. Strictly
  additive; never clobbers operator edits.

## [0.5.0] — 2026-07-04

Self-describing record–schema binding and schema versioning (Phase 20 / ADR-0019).
Records now carry their validation binding at rest: a server-stamped
`dct:conformsTo` → a `prof:Profile` (W3C Profiles Vocabulary), backed by immutable,
versioned schema/profile identity and version provenance in the meta graph.

### Added

- **Self-describing `dct:conformsTo` on every record.** On `PUT`/`POST`/`PATCH`
  the server stamps `dct:conformsTo` → the type's stable profile IRI into the
  record graph. The binding is server-owned: a client-supplied `conformsTo` into
  the managed profile namespace is replaced with the type default (ADR-0019 §2),
  while a `conformsTo` to any other vocabulary is preserved. (ADR-0019 §1)
- **`fdp-o:validatedAgainst` version provenance.** The exact profile *version* a
  record was validated against is recorded in its `<record>/meta` graph, so a
  restore/import can reproduce the original validation. (ADR-0019 §3)
- **PROF conformance profiles.** A new read-only surface at `/fdp-api/profiles`
  (`GET` list / `{id}` / `{id}/{version}`). A profile is the 1:1 wrapper of a
  SHACL schema (`prof:hasResource` with `prof:hasRole role:validation` →
  the schema's immutable version snapshot), auto-provisioned on schema publish.
- **Immutable, versioned schemas.** Publishing a schema now snapshots an immutable
  version graph at `…/fdp-api/schemas/{slug}/{version}` (retained across edits;
  the stable IRI keeps serving the current shape). Fetch a snapshot via
  `GET /fdp-api/schemas/{id}/{version}`. (ADR-0019 §4)
- **`fdp profile backfill-conformance`.** One-shot, idempotent, non-destructive
  migration: provisions the profile (+ version snapshot) for every managed schema
  and stamps `dct:conformsTo`/`fdp-o:validatedAgainst` on existing records of a
  known type — without bumping their version. A fresh bootstrap does this
  automatically for seeded schemas and records. (ADR-0019 §6)

### Changed

- **`meta-metadata.ttl`** gains an optional `fdp-o:validatedAgainst` (IRI); the
  base **`resource.ttl`** annotates `dct:conformsTo` as the server-managed binding
  (multiple values allowed). No shape is `sh:closed`, so the stamped triples
  validate cleanly.
- **ResourceDefinition** is now a type→profile index (validation resolves through
  the record's `conformsTo`). Implementation note: the RD's `ldp:constrainedBy`
  stays on the schema and the profile is derived 1:1 from it — same self-describing
  outcome, far less churn than flipping `constrainedBy` (ADR-0019 §2, amended).

## [0.4.0] — 2026-07-03

Alternative identifiers and FAIR Signposting (Phase 17): write-path hardening, the
`owl:sameAs` → `adms:identifier` semantics change, FAIR Signposting Level 1 `Link`
relations on reads, and lifecycle/consistency fixes — on a repaired and
GraphDB-backed integration suite.

### Changed — LDP contract (⚠ behaviour changes)

- **Ambiguous request body → `400`.** `reconcile_identifiers` no longer has a
  "store as authored" fallback. A write body (`PUT`/`POST`) must address the
  record as `<>` / its canonical IRI, or contain exactly one typed IRI subject
  (which is rebound to the canonical IRI). Zero typed subjects, several, or a
  blank-node-only body is rejected with `400 fdp.ambiguous_subject` — the
  canonical-subject invariant is now unconditional. (ADR-0016 §1)
- **`POST` `Slug` collision → `409`.** `POST` to a container never overwrites: if
  the slug-derived member IRI already exists, the server responds
  `409 fdp.conflict` so the client picks another `Slug` or uses `PUT` + `If-Match`
  deliberately. (ADR-0016 §1)
- **Foreign brought-along identifier → `adms:identifier`, not `owl:sameAs`.** When
  a write rebinds a foreign primary subject to the canonical IRI, the original is
  now recorded as a structured alternative identifier — `dct:identifier` (literal)
  plus an `adms:identifier` node (`adms:Identifier` with `skos:notation`
  `^^xsd:anyURI`). The server no longer mints `owl:sameAs` (it cannot know the two
  resources are identical). Consumers that joined on the server-added `sameAs`
  must switch to `adms:identifier`/`dct:identifier`. Existing server-minted
  `sameAs` triples are **not** migrated (indistinguishable from client-authored
  ones); the resource SHACL shape gains an optional `adms:identifier`. (ADR-0017 §1)

Clients that relied on the previous lenient behaviours (silent "store as
authored", silent `POST` overwrite) must switch to the explicit forms above.

### Fixed

- **Managed resources can now be published via the shared `/state` endpoint.**
  Policies, licenses, schemas and resource definitions have canonical IRIs under
  the reserved namespace (`<base>/fdp-api/<segment>/<id>`), but the
  publication-state router — mounted under `/fdp-api` — resolved the
  prefix-stripped sub-path to a non-existent root IRI and returned `404`. A new
  `shared.graphs.state_record_iri` re-adds the reserved prefix for managed
  segments (and passes root-level LDP records straight through), so
  `POST /fdp-api/policies/{id}/state` (and licenses/schemas/RDs) now works.
- **Repository-root container listing.** `GET /fdp-api/page/{childPrefix}` at the
  repository root reported zero children even when catalogs existed: the handler
  queried the root subject with a trailing slash while records are stored under
  the slash-stripped IRI. The lookup is now normalized via `record_graph_uri`.

### Added

- **FAIR Signposting (Level 1).** Every `GET`/`HEAD` of a record now carries typed
  RFC 8288 `Link` relations — `cite-as`, `describedby` (per RDF media type),
  `type`, `license`, `author`, `item`, `collection` — so an agent can navigate and
  cite a record from headers alone. `cite-as` gives a client-supplied PID
  (`owl:sameAs` / `adms:identifier` / IRI-valued `dct:identifier` under a
  recognised resolver, or an `ark:` IRI) citation primacy over the canonical IRI.
  Capped per response; the Level-2 `linkset` document is deferred. See
  `docs/conformance/signposting-conformance.md`. (ADR-0017 §2)
- **`adms:identifier`** joins the namespace registry and the resource SHACL shape
  (optional), backing the alternative-identifier model above.
- **Full-stack deployment** (`deploy/stack/`): one `docker compose` command
  brings up client + server + GraphDB + Postgres + Keycloak with automatic
  bootstrapping. Keycloak health is probed on the management port (9000); the
  client is published on `5173` (the origin the bundled realm allows for OIDC
  redirects); `EXPOSE_API_DOCS` serves the Swagger/ReDoc UIs in the evaluation
  stack. The OpenAPI spec is always served at `/fdp-api/openapi.json`.
- **ADR-0016** (faithful backup/restore & instance migration) and **ADR-0017**
  (structured alternative identifiers + FAIR Signposting) recorded. ADR-0016 §1
  (write-path hardening) and all of ADR-0017 ship in this release; the remaining
  ADR-0016 work (`fdp dump`/`restore`/`import`) is scheduled for a later phase.

### Documentation

- README documents the one-command full-stack path, the always-on OpenAPI spec
  URL, and the `EXPOSE_API_DOCS` flag.
- Dev docs: the request lifecycle now documents the `400`/`409` write-path
  invariants and the signposting header stage; new FAIR Signposting conformance
  note at `docs/conformance/signposting-conformance.md`.

### Tests / infrastructure

- Shared **GraphDB testcontainer fixture** (`tests/integration/conftest.py`) for
  the end-to-end tests that exercise multi-graph SPARQL projection, which
  requires a store honouring named-graph isolation (Oxigraph does not).
- Repaired integration e2e tests that used pre-`/fdp-api` endpoint paths
  (`/search`, `/sparql`, `/{record}/state`) and stale profile-apply / schema
  expectations. Schemas are referenced by their storage IRI
  (`<base>/fdp-api/schemas/<slug>`); profile manifests still declare them by
  CURIE, resolved by the applier.

## [0.3.0]

Persistent identifiers (FAIR F1): identifier-base / serving-origin split, dual
identifier model, and `fdp pid` tooling. See ADR-0014.

## [0.2.0]

DCAT v3 modular schemas and full LDP conformance. See Phase 15.
