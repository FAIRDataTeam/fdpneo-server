# Initial implementation tasks — fdp-server

This is the suggested order for building the server. Each task is sized to
fit one focused Claude Code session, ends in something runnable or testable,
and leaves the codebase in a working state.

Read [`CLAUDE.md`](CLAUDE.md) before starting anything. The architecture is
fully specified in `docs/architecture/README.md` and the eight ADRs in
`docs/adr/` — refer to them, do not improvise around them.

## How to use this list

- Tasks are roughly ordered. Earlier ones unblock later ones.
- Each task lists the architectural references that apply.
- Done = code merged, tests passing, public surface documented.
- If you find a task underspecified, surface the ambiguity rather than guessing.
- Each task is prefixed with a status marker: `[x]` completed, `[~]` partially done, `[ ]` not started.

---

## Status snapshot (2026-05-29)

**Phases 0 – 5 (initial build): complete.** The server starts, applies the
default profile on first run, serves a 31-path OpenAPI spec covering the
five DCAT metadata kinds, exposes `/sparql` with read/write authorization,
the per-distribution data router, and an anonymous metrics pipeline with
dashboard read endpoints. Unit suite green (461/461). The `/openapi.json`
spec is consumable by `fdp-client`'s `npm run generate-api`. Two `main.py`
TODOs remain (anon authz-cache warming, SHACL-on-write hook) — both
deferrable, not blockers.

The next phases (6 – 13) are **parity work against the Java reference
implementation** at <https://github.com/FAIRDataTeam/FAIRDataPoint>. That
implementation has years of accumulated client-facing surface this new
server has not yet replicated. The phases below cover the gaps that matter
for `fdp-client` UX and for cross-FDP interoperability. Items deliberately
NOT being ported are listed at the bottom with the ADR that justifies the
omission.

---

## Phase 0 — Foundations

These produce the skeleton everything else builds on.

### 0.1 [x] Shared kernel: namespaces, errors, request context
- Implement `shared/namespaces.py` with the standard RDF prefix registry (DCAT, DCT, FOAF, LDP, ODRL, PROV, SH, XSD, plus an `fdp:` namespace).
- Implement `shared/errors.py` with `FDPError` base class, a small set of concrete errors (NotFound, Forbidden, Conflict, SchemaViolation, PolicyViolation), and the FastAPI exception handler that maps them to a structured JSON envelope.
- Implement `shared/context.py` with the immutable `RequestContext` dataclass (subject URI or anonymous sentinel, role set, request timestamp, trace ID).
- Implement `shared/logging.py` configuring structlog with the request-context binding.
- Implement `shared/events.py` — a minimal async in-process event bus (publish/subscribe with weak references).
- Unit tests for each module.

References: CLAUDE.md (Code conventions, RDF and namespaces), architecture §5.7, §14.1.

### 0.2 [x] Postgres adapter with Alembic
- SQLAlchemy 2.x async engine and session factory in `storage/postgres/engine.py`.
- Alembic environment in `migrations/` configured to read the URL from settings.
- Initial migration creating empty tables for: `metrics_hourly`, `metrics_daily`, `authz_index`, `policy_decisions_audit`, `job_state`, `profile_applied`. Schema details TBD per consuming module; this migration just establishes the namespace.
- Integration test using testcontainers-postgres that runs the migration cleanly.

References: ADR-0003, architecture §4.4, §5.8.

### 0.3 [x] Triple store adapter (SPARQL 1.1 Protocol)
- `storage/triplestore/adapter.py` exposing `query`, `update`, `ingest_graph`, `replace_graph`, `drop_graph`, `ask`.
- Use `httpx.AsyncClient`. Authentication via basic or bearer per configuration.
- Capability flags read from `TripleStoreSettings`; default implementations raise `NotImplementedError` for capabilities a backend lacks.
- Integration tests against testcontainers-launched GraphDB, Fuseki, and Oxigraph. The same test suite runs against all three; backends that lack a capability are skipped via marker.

References: ADR-0005, ADR-0007, architecture §4.3, §5.8.

---

## Phase 1 — Identity and access foundations

### 1.1 [x] OIDC authentication middleware
- `identity/jwks.py` — JWKS fetch and cache against the configured issuer's OIDC discovery document.
- `identity/middleware.py` — FastAPI middleware that extracts the bearer token, validates it, builds the `RequestContext`, and attaches it to the request.
- `identity/deps.py` — FastAPI dependencies: `current_context()`, `require_auth()`.
- Use `respx` to mock the IdP in tests. No live Keycloak in unit tests.

References: ADR-0001, architecture §5.2, §7.

### 1.2 [x] ODRL evaluator core (no inheritance yet)
- `policy/model.py` — Pydantic / dataclass models for the FDP ODRL profile (Offer, Permission, Prohibition, Action, supported Constraint types).
- `policy/parser.py` — parse a graph of `odrl:Offer` triples into the model. Reject anything outside the profile with a clear error pointing at the offending triple.
- `policy/evaluator.py` — pure-function evaluator: given a parsed Offer and a `RequestContext` plus a requested action, return `Decision` (PERMIT/DENY) and the rule that fired.
- Conflict resolution: deny wins by default, overridable per policy.
- Exhaustive unit tests with hand-rolled Offers covering every supported constraint type and conflict scenario.

References: ADR-0006, architecture §8.

### 1.3 [x] Authorization cache and PDP wiring
- `policy/cache.py` — SQLAlchemy model for the `authz_index` table; repository methods for upsert and bulk lookup.
- `policy/pdp.py` — public `authorize(subject, action, resource)` and `authorized_graphs(subject, action)` functions that read from the cache and lazily populate it.
- Invalidation hooks: synchronous on policy write (called by metadata module), asynchronous on user role change.
- Integration tests using real Postgres via testcontainers.

References: architecture §8.5, §9.4.

---

## Phase 2 — Metadata provider with LDP

### 2.1 [x] RDF graph CRUD via triple store adapter
- `metadata/graphs.py` — typed helpers for the per-record / per-meta / per-audit graph URI conventions.
- `metadata/repository.py` — `get_graph`, `put_graph`, `patch_graph` (apply SPARQL Update scoped to one graph), `delete_graph` plus the meta-metadata updates.
- ETag computation: canonicalize triples to N-Triples sorted, hash with BLAKE2b.

References: architecture §6, ADR-0007.

### 2.2 [~] SHACL validation pipeline
- `metadata/shacl.py` — wraps pySHACL with a fast-path for cached compiled shapes.
- `validate_against(graph, shape_iri)` returning a structured violation report or success.
- Profile bootstrap pre-compiles known shapes; runtime falls back to compile-on-first-use.

References: architecture §10.1, §13 (server-side validation).

### 2.3 [x] LDP server skeleton
- `metadata/ldp/router.py` — FastAPI router with GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS for resources and containers.
- Content negotiation across Turtle, JSON-LD, RDF/XML, N-Triples.
- ETag + `If-Match` for concurrency control.
- Link headers advertising LDP types.
- Per-method PEP calls into `policy.authorize`.

References: ADR-0008, architecture §10.

### 2.4 [x] PATCH via SPARQL Update
- Parse the body with RDFLib.
- Authorize as `odrl:modify` on the target graph (implicit from URL).
- Apply to a virtual copy, run SHACL against the post-update state.
- On success, commit + update meta-metadata + emit `record.modified` event.
- Reject with 422 + violation report on SHACL failure; reject with 403 on policy denial.

References: architecture §10.3.

### 2.5 [x] Meta-metadata management
- `metadata/meta.py` — generates the meta-metadata graph on create and updates it on every modification.
- Validate the meta-metadata graph against the configured meta-metadata schema on every write.

References: architecture §6.2.

### 2.6 [x] LDP read-extension endpoints (`/spec`, `/expanded`, `/page`)
`metadata/openapi.py` already *documents* three read extensions per resource
definition, but no runtime handler implements them — requests fall through to
the catch-all `/{path:path}` LDP router and return 404/401. `fdp-client` needs
at least `/spec` to render SHACL-driven create/edit forms (its task 7.5) and
currently works around the missing `/expanded` / `/page` with SPARQL.

- **`GET /{urlPrefix}/spec` (type-level) + `GET /spec` (root)** — return the SHACL
  NodeShape graph that validates members of that type. **All the pieces already
  exist:** shapes are stored at their CURIE-expanded class IRI (e.g.
  `http://www.w3.org/ns/dcat#Catalog`, ~52 triples), `MetadataShapeProvider.fetch`
  reads a shape graph, and the resource-definition cache maps each type → its
  `schema_iri`. So: resolve type → `schema_iri` → fetch graph → content-negotiated
  RDF, anonymous-readable. A *type-level* variant (no `{id}`) is what create forms
  need, since no instance exists yet — the currently-documented `/{urlPrefix}/{id}/spec`
  is instance-level only.
- **`GET /{urlPrefix}/{id}/expanded`** — record + all `dct:isPartOf` ancestors.
- **`GET /{urlPrefix}/page/{childPrefix}`** — paginated children listing. (The
  client now lists via SPARQL `GRAPH ?g`; a real endpoint would remove that
  workaround and the named-graph-name coupling.)

Wire these as explicit routes registered **before** the `/{path:path}` catch-all
so they aren't shadowed. `/spec` is the priority — it is the sole blocker for
client task 7.5 (SHACL-driven forms); without it the client falls back to its
config-driven `EntityForm`.

References: `metadata/openapi.py` (already-documented paths), `metadata/shape_provider.py`, `metadata/profiles/registry.py` (resource-definition cache → `schema_iri`), architecture §10.

---

## Phase 3 — SPARQL endpoint

### 3.1 [x] Query parser and classifier
- `access/parser.py` — parse with RDFLib's algebra; classify as read/update; enumerate referenced graphs (FROM, FROM NAMED, GRAPH clauses, WITH for updates).
- Reject SERVICE clauses with 400 + clear message.
- Reject anonymous updates with 401.

### 3.2 [x] Query rewriter
- `access/rewriter.py` — inject `FROM NAMED <g>` for each graph in the user's authorized read set, intersected with any explicit references.
- For updates: validate explicit targets against `authorized_graphs(subject, "modify")`. Reject ambiguous-target updates with 400 + remediation hint.

### 3.3 [x] SPARQL endpoint API
- `access/router.py` — FastAPI router at `/sparql` accepting GET and POST, all standard result formats.
- Stream large CONSTRUCT/DESCRIBE results.

References: ADR-0004, architecture §9.

---

## Phase 4 — Metrics

### 4.1 [x] Anonymization layer
- `metrics/anonymize.py` — pure function that transforms a raw event (with IP, identity, UA, query text) into an aggregate-safe event (country/region/city, daily visitor hash, event type, resource id, timestamp bucket). Drops everything else.
- Daily salt rotation in memory with monotonic clock.
- Property-based tests verifying the function never emits an identifying field downstream.

### 4.2 [x] Event-bus subscriber and aggregation
- `metrics/pipeline.py` — subscribes to the in-process event bus through `anonymize`, writes raw events to a short-retention Postgres table.
- Hourly rollup job (arq) condenses raw → hourly → daily.

### 4.3 [x] Dashboard API
- `metrics/api.py` — read-only endpoints serving the client's dashboard charts. Stewards see their resources; admins see system-wide.

References: ADR-0002, architecture §11.

---

## Phase 5 — Simple data provider and profile bootstrap

### 5.1 [x] Data provider for open-access distributions
- `data/router.py` — serves files via `downloadURL` (stream or redirect per config) and exposes per-distribution scoped SPARQL endpoints via `accessURL`.
- v1 serves only distributions whose Offer permits anonymous read.

References: architecture §5.6.

### 5.2 [x] Profile bootstrap
- Implement the four `fdp profile *` CLI commands wired against `metadata/profiles.py`.
- Validation: SHACL shapes parse, ODRL Offers conform to the FDP profile, seed records validate, container references resolve.
- Apply: schemas → containers → offers → seed records, in a single transaction at the storage layer.
- Refuse re-apply unless `--force` is passed AND a confirmation prompt is answered.

References: architecture §12.

---

## Open items deliberately left for later

- LD-PATCH support (currently SPARQL Update PATCH only).
- IdP role-to-FDP-role mapping configuration.
- Access-controlled data delivery.
- Property-level (sub-record) access control.
- ODRL Duty / obligation enforcement.

See `docs/architecture/README.md` §15 for the complete open-questions list.

---

# Reference-implementation parity (Phases 6+)

The Java reference implementation at
<https://github.com/FAIRDataTeam/FAIRDataPoint> exposes many controllers
this server has not yet replicated. Below is the audit and the staged plan
to close the gaps that matter for `fdp-client` UX and for cross-FDP
interoperability with the existing FAIR Data Point ecosystem.

**Methodology.** I walked the reference repo's controller, service, and
DTO tree (471 Java files) on 2026-05-29, classified every public surface,
and mapped each one to (a) an equivalent already shipped here, (b) an
equivalent intentionally replaced by a different architecture (cited
below with the ADR), or (c) a gap that needs porting. The phases below
cover (c). The reference repo was on commit `0fe214b` of the `develop`
branch at audit time.

## Phase 6 — Client-UX surface (highest priority)

These four endpoints are the most visible gaps. Without them, the client
can render lists and edit forms but cannot offer the kind of polish the
existing FAIRDataPoint-client expects.

### 6.1 [x] Labels endpoint (`GET /labels`)

The reference impl exposes `LabelController` + `LabelService` (cache of
IRI→human-readable label, populated from RDF graphs and remote
vocabularies). The client uses this to render `dct:license` IRIs as
"Creative Commons Attribution 4.0" instead of as raw URLs.

- `metadata/labels.py` — service that, given a list of IRIs, returns the
  best available `rdfs:label` / `skos:prefLabel` / `dc:title`. Cache is
  per-language with TTL.
- `GET /labels?iri=<...>&iri=<...>` — batched lookup, returns
  `{ "labels": { "<iri>": "<label>" } }`. Missing IRIs are omitted.
- The local triple store is checked first; an optional remote-vocabulary
  resolver (config-driven allow-list) is the secondary source. No
  unauthenticated outbound fetches to arbitrary IRIs.

References: `LabelService.java` (reference impl), architecture §10
(public read surface).

### 6.2 [x] Form autocomplete (`GET /forms/autocomplete`)

The reference impl exposes `FormAutocompleteController` driving form
widgets that let a curator pick a value from a managed list — e.g. pick
a license from a curated set of licenses, or a publisher from a list of
known organisations. The endpoint is admin-configurable (see Phase 9.3
for the *sources* CRUD).

- `metadata/autocomplete.py` — given `{source: "license", prefix: "cc"}`
  return `[{iri: ..., label: ..., source: "license"}, ...]`. Sources are
  named at the deployment-profile level; each maps to either an inline
  list, a SPARQL query, or a remote vocabulary URL.
- Default sources (DCAT licenses, DCAT publishers, MIME types) ship with
  the bundled profile.
- The endpoint is unauthenticated (public read) so the client can wire
  forms without an extra round-trip to the IdP, but the *sources
  management* endpoints in Phase 9.3 are admin-only.

References: `FormsAutocompleteService.java`,
`SettingsAutocompleteSourceDTO.java`.

### 6.3 [x] User dashboard (`GET /me/dashboard`)

The reference impl's `DashboardController` returns the records the
current user owns, can edit, or has recently modified — the data
backing "My data" screens in the client.

- `metadata/dashboard.py` — for the current `RequestContext`,
  enumerate records where the subject has `modify` or `delete`
  permission according to the PDP (`authorized_graphs(subject,
  "modify")` already exists; add a `recently_modified_by(subject,
  limit)` reader).
- `GET /me/dashboard` returns `{owned: [...], editable: [...],
  recent: [...]}` with each item carrying record IRI, type IRI,
  `dct:title`, last-modified timestamp, and a meta-state if Phase
  12 has shipped.
- Anonymous → 401. Authenticated callers see only their own records;
  admins see system-wide via a query param.

References: `DashboardService.java`, `DashboardItemDTO.java`.

### 6.4 [x] Bootstrap config (`GET /config`)

The reference impl exposes `ConfigController` (`/api/v1/config`)
returning `BootstrapConfigDTO` — the FDP's self-description that the
client reads at startup to know which IdP to point users at, which
features are enabled, etc.

- `identity/bootstrap.py` — read from settings + applied-profile state.
- `GET /config` returns:
  ```json
  {
    "fdp_url": "<base_url>",
    "fdp_namespace": "<...>",
    "oidc": {"issuer": "...", "audience": "...", "client_id_hint": "fdp-client"},
    "profile": {"name": "default", "version": "0.1.0"},
    "features": {"search": true, "index": false, "metrics": true}
  }
  ```
- Unauthenticated (the client needs it pre-login). No secrets.

References: `ConfigService.java`, `BootstrapConfigDTO.java`.

---

## Phase 7 — Search

The reference impl has `SearchController` with text+filter search and
`SearchSavedQueryController` for stored queries. The client's main
discovery page uses this; without it, users can only navigate by typed
container.

### 7.1 [ ] Search index and ingestion pipeline

- New Postgres table `metadata_search` holding one row per record:
  `(record_iri, type_iri, title, description, search_text tsvector,
  updated_at, language)`. The `search_text` is a `tsvector` built from
  title, description, and selected literals; language defaults to
  `english` (configurable).
- `metadata/search/indexer.py` subscribes to `record.modified` and
  `record.created` events from the existing event bus
  (`shared/events.py`) and upserts the row.
- A one-shot CLI (`fdp search reindex`) walks every record in the
  triple store and rebuilds the index. Used after schema or profile
  changes.

### 7.2 [ ] Search query API

- `metadata/search/router.py` — `POST /search` with body
  `{query: "...", filters: [...], types: [...], offset, limit,
  language}` returning `{items: [...], total, facets: {...}}`.
- Filters: by `type_iri`, by `dct:license`, by date range. Facets returned
  per filter dimension (count by type, by license).
- PDP gating: only records the subject can `read` are returned; the
  filter is applied via `authorized_graphs(subject, "read")` joined into
  the SQL WHERE.
- Query is parameterized via SQLAlchemy; no string interpolation per
  CLAUDE.md "SPARQL strings are parsed, never interpolated".

### 7.3 [ ] Saved queries

- Postgres table `search_saved_queries(id, owner_subject, name, query_json,
  shared, created_at)`. Owner-only by default; admins can mark a query
  `shared=true` to make it visible to everyone.
- CRUD endpoints: `GET /me/saved-queries`, `POST /me/saved-queries`,
  `PUT/DELETE /me/saved-queries/{id}`. Sharing toggle is admin-only.

References: `SearchService.java`, `SearchSavedQueryService.java`,
`SearchFilterCache.java`.

---

## Phase 8 — FDP Index protocol (cross-FDP discovery)

The reference impl is the canonical implementation of the FAIR Data
Point Index protocol — FDPs ping a registered Index with their URL, the
Index pulls their root catalog metadata, and the Index exposes a
directory of known FDPs. Without this, a deployed FDP cannot be
discovered by the community indexes (e.g. `https://home.fairdatapoint.org`).

Decide before starting: do we want THIS server to be (a) an Index that
collects pings from other FDPs, (b) a regular FDP that pings an Index,
or (c) both? The reference impl supports both modes via the
`INDEX_FEATURE_ENABLED` flag.

### 8.1 [ ] Index entries model and storage

Only if running as an Index. Postgres tables: `index_entries`,
`index_events`, `index_webhooks` mirroring the reference repository
models. An entry stores `(client_url, state, last_retrieval_at,
metadata_iri, version, …)`. State machine: UNKNOWN → VALID → INVALID →
EXPIRED.

### 8.2 [ ] Incoming Ping endpoint (`POST /index/ping`)

Unauthenticated. Body `{clientUrl: "https://other-fdp.example/"}`.
Records the ping, enqueues a `MetadataRetrieval` event so the harvester
fetches `clientUrl/spec` and stores the entry's metadata. Rate-limited
per source IP to mitigate ping floods.

### 8.3 [ ] Outbound Ping service

Only if running as a regular FDP. A scheduled job (arq or the existing
metrics rollup pattern) POSTs `{clientUrl: settings.base_url}` to every
configured Index URL on a configurable interval (default 7 days).
Settings: `FDP_INDEX_PING_TARGETS=https://a.example,https://b.example`,
`FDP_INDEX_PING_INTERVAL_SECONDS=604800`.

### 8.4 [ ] Index admin API

Admin endpoints to list/inspect/permit/forbid index entries and replay
events. Behind `policy.authorize(admin, manage, "fdp:index")`.

### 8.5 [ ] Webhooks

When an index entry changes state (VALID → INVALID, etc.), POST to
configured webhook URLs with a signed payload. Supports retry with
backoff. Persisted in `index_webhooks` for audit.

References: `IndexEntryService.java`, `EventService.java`,
`HarvesterService.java`, `WebhookService.java`, `IncomingPingUtils.java`.

---

## Phase 9 — Runtime settings (admin)

The reference impl lets admins reconfigure several things without a
redeploy via `SettingsController`. Whether we want the same is an open
question — our deployment-profile model already covers most of this via
re-apply. Where the reference admin API adds genuine value is for things
that change frequently (ping target list, search filter visibility,
autocomplete sources) and shouldn't require touching the profile bundle.

### 9.1 [x] Settings storage and read API

Postgres table `runtime_settings(key, value_json, updated_by, updated_at)`.
`GET /settings` returns the merged view (defaults from the profile,
overlaid with runtime overrides). Public — the client reads it on every
load.

### 9.2 [x] Settings update API (admin-only)

`PUT /settings/<key>` with a JSON body. Validated against a Pydantic
model per key. Audit-logged.

### 9.3 [x] Forms-autocomplete sources management

`GET/POST/PUT/DELETE /settings/forms/autocomplete-sources`. Each source
declares its name, type (`inline | sparql | vocabulary-uri`), and the
payload (the list of items, the SPARQL query, or the URI). Feeds Phase
6.2.

### 9.4 [~] Search filter configuration

`GET/PUT /settings/search/filters` — declare which filter dimensions are
exposed on the search page, their labels, and their facet expansion.

References: `SettingsController.java`, `SettingsService.java`,
`SettingsFormsAutocompleteDTO.java`, `SettingsSearchDTO.java`.

---

## Phase 10 — Schema and ResourceDefinition runtime admin

The reference impl exposes `MetadataSchemaController` (CRUD over SHACL
shapes with versions and drafts) and `ResourceDefinitionController` (CRUD
over the typed-record definitions).

ResourceDefinitions are now runtime-mutable (10.3, done — see
[ADR-0009](docs/adr/0009-runtime-resource-definitions.md)). SHACL schemas
are still published as ordinary LDP records (PUT to a deployment-relative
IRI); a dedicated schema-admin surface with drafts/release (10.1) is not
yet built. The profile bundle remains the *seed* and the reproducibility
artefact: bootstrap writes its `resourceDefinitions` into the store, and
runtime changes layer on top without a re-apply.

### 10.1 [x] Schema admin API

Runtime SHACL-shape management in `src/fdp/metadata/schemas.py`
(`SchemaService` + `build_schema_router`), mounted at `/schemas` before the
LDP catch-all. Surfaces:

- `GET  /schemas` — list published shapes (id, IRI, target class); public.
- `GET  /schemas/{id}` — the shape as Turtle; public.
- `PUT  /schemas/{id}` — create/replace (Turtle body); **admin**. Validates
  the body parses and is a SHACL shape (`sh:NodeShape`/`sh:targetClass`, plus
  a pySHACL load-check), stores it as a record at `{base}/schemas/{id}`, and
  invalidates + re-warms the validator cache.
- `DELETE /schemas/{id}` — **admin**; refused (409) if a resource definition
  still references the shape via `ldp:constrainedBy`.
- `POST /schemas/{id}/validate` — dry-run a sample record against the shape,
  returns the SHACL report; authenticated.

Versioning comes from the meta-writer (`owl:versionInfo` bumps per write) at a
**stable IRI**, so resource-definition `schema` references stay valid across
edits. Closes the two-step-flow gap: a steward can now publish a shape, then
register a type pointing at it (10.3) entirely through the API. Auth mirrors
settings/resource-definitions (public reads, admin writes). Tests:
`tests/unit/metadata/test_schemas.py` (15 — service with real pySHACL +
router auth gating); live-smoked on the GraphDB dev stack.

Deferred (overlaps Phase 12 publication-state, not needed for the two-step
flow): draft/released lifecycle, version-history browsing, rollback.

### 10.2 [x] Remote schema synchronization

A schema can declare `dct:source <remote URL>` and the server
periodically refetches and bumps the version. Disabled by default;
opt-in per-schema.

Delivered in `src/fdp/metadata/schema_sync.py` (`SchemaSyncService`):

- **Discovery** — a SPARQL scan of `{base}/schemas/*` for a `dct:source` IRI;
  schemas without one are never touched (per-schema opt-in).
- **Refetch + change detection** — fetches the source (size-capped streamed
  read; Turtle/RDF-XML/JSON-LD by content-type), and compares against the
  stored shape by **RDF graph isomorphism** (`rdflib.compare`), not bytes —
  SHACL shapes are blank-node heavy, so a byte/ETag compare would report false
  changes on every run. The bookkeeping `dct:source` triple is excluded from
  the comparison and re-stamped canonically on the schema record IRI when
  republishing, so it neither triggers a spurious change nor gets lost.
- **Version bump** — a real change is republished through the 10.1
  `SchemaService.put` admin path, which bumps `owl:versionInfo` at the stable
  IRI (resource-definition `schema` refs stay valid) and re-warms the validator.
- **Disabled by default, two gates** — `FDP_SCHEMA_SYNC_ENABLED=false` (the
  scheduled pass) and `FDP_SCHEMA_SYNC_ALLOWED_HOSTS` (empty ⇒ no fetch
  allowed). The host allow-list is the structural "no unauthenticated outbound
  fetches to arbitrary IRIs" boundary and is enforced on **every** fetch,
  including a manual run. Per-schema failures are collected/logged, never
  aborting the batch (`SyncReport` with updated/unchanged/skipped/failed).
- **Scheduling** — a plain async pass invoked by `fdp schema sync` (new CLI
  command) from an external scheduler (cron / k8s `CronJob`) on
  `FDP_SCHEMA_SYNC_INTERVAL_SECONDS`, mirroring how `fdp metrics rollup` is
  scheduled. `--force` runs it past the `enabled` gate (allow-list still applies).
- New `SchemaSyncSettings` config group (`FDP_SCHEMA_SYNC_*`).

Side fix in `schemas.py` (10.1 latent bug): `_parse_shape` let pySHACL
normalize the shapes graph **in place** (injecting RDFS/OWL axioms) even with
`inplace=False`, so the *stored* shape was polluted vs. the authored one —
leaking noise into `GET /schemas/{id}` and defeating sync's stored-vs-fetched
compare. Now validates against a throwaway copy; the stored graph is pristine.

Tests: `tests/unit/metadata/test_schema_sync.py` (10 — discovery, allow-list
gate, isomorphic-UNCHANGED on a re-serialized blank-node shape, UPDATED +
source re-stamp, HTTP/parse failures, aggregate counts, config defaults/CSV)
over the real `SchemaService` with `respx`-mocked fetches. Full unit suite
green (678).

Remote-sync of a schema that is itself remotely-versioned (chained sources)
and an in-process scheduler are out of scope; the CLI + external scheduler is
the shipped cadence, consistent with metrics.

### 10.3 [x] ResourceDefinition admin API

Done, and broader than originally scoped — see
[ADR-0009](docs/adr/0009-runtime-resource-definitions.md). Delivered:

- Resource definitions are stored as RDF records (one named graph each,
  reserved `…/resource-definitions/` namespace), seeded at bootstrap and
  runtime-mutable thereafter — `metadata/profiles/rd_records.py` (vocab +
  predefined SHACL shape), `rd_service.py` (store reads + the
  `ResourceDefinitionService` mutation coordinator), `registry.resolve_cache`
  (cache is now a projection of the store, rebuilt on startup + every mutation).
- Router `metadata/rd_api.py` (`/resource-definitions`): public read catalog
  (`GET` list + `GET /{slug}`, anonymous — feeds the client's dynamic type
  catalog) and admin-gated `POST`/`PUT`/`DELETE`. `PUT` replaces a definition
  including its child links — that's how a child link is added to an existing
  type (Catalog → a new Ontology type). Validates schema existence + url-prefix
  uniqueness + reserved-path collisions; protects the root from deletion.
- On a successful mutation the cache is swapped, `app.openapi_schema` cleared,
  and the validator + anonymous-authz caches warmed, so the new type's LDP
  endpoints and OpenAPI paths light up with no restart
  (`main._publish_resource_definitions`).
- Internal-graph isolation invariant: a single `is_internal_graph_uri`
  (`shared/graphs.py`) keeps RD/meta/audit graphs out of the public SPARQL
  projection (`PDP.authorized_graphs`) even though RD records are REST-readable.
- Auth: mutations require the `admin` role (RDs are deployment config, like
  runtime settings), not the ODRL PDP.

Tests: unit coverage complete (rd_records, rd_service, rd_api, graphs,
pdp-exclusion). Integration tests in
`tests/integration/metadata/test_runtime_resource_definitions.py`
(testcontainers Oxigraph + Postgres) **pass (4/4)** — covering the
end-to-end ontology scenario, member creation, restart-from-store
persistence, and internal-graph isolation (RD record REST-readable but its
graph forbidden to anonymous SPARQL via §9.5).

Side finding (investigated; NOT an RD-feature bug): the `/sparql` access
endpoint mis-behaved against **Oxigraph** for multi-graph projected reads.
Root-caused to two Oxigraph SPARQL-Protocol non-conformances with repeated
`named-graph-uri`:

1. **Rejected in the form body** — the adapter sent the query + dataset params
   as a urlencoded POST body (§2.1.2); Oxigraph 400s on repeated
   `named-graph-uri` there. **Fixed:** `TripleStoreAdapter.query` /
   `query_stream` now use "query via POST directly" (§2.1.3) — raw query body
   + dataset params in the URL query string (matching `update()`). Unit +
   access-router tests updated.
2. **No dataset union in the URL either** — given multiple `named-graph-uri`
   Oxigraph honours only the *last* one, so an authorized set of >1 graph
   under-projects. Verified this is Oxigraph-specific: **Fuseki unions
   correctly** (tested), as do GraphDB (recommended default) and the SPARQL
   1.1 Protocol spec. So the named-graph-projection design (ADR-0004) is sound
   on conformant stores; Oxigraph's protocol layer is the outlier.

Consequence: the access-control `/sparql` endpoint requires a backend that
unions repeated `named-graph-uri` (GraphDB, Fuseki). **Oxigraph is dev-only
for this endpoint** (consistent with ADR-0005's "Oxigraph for development").
Follow-up options if Oxigraph must be fully supported: textual `FROM NAMED`
injection in the rewriter (works on Oxigraph — verified), or a capability flag
that selects the projection strategy per backend. Not done here — it's an
ADR-0004/0005 decision, not part of the RD feature.

### 10.4 [x] Reset to factory defaults

`POST /admin/reset` — admin-only and destructive. Truncates the
Postgres `runtime_settings` table and re-applies the bundled profile
with force. Confirmation token required in body. Audit-logged.

Delivered in `src/fdp/metadata/admin.py` (`ResetService` +
`build_admin_router`), mounted at `/admin` before the LDP catch-all (added
to the reserved-prefix list in `rd_api.py` and the `main.py` comment):

- `POST /admin/reset` — **admin** (role check, like settings / resource
  definitions; not the ODRL PDP). Body must carry the fixed confirmation
  token `reset-to-factory-defaults` (`RESET_CONFIRMATION_TOKEN`); a
  missing/wrong token is rejected **before** anything is touched (422 / 400).
- Flow: load + structurally validate the bundle first (a broken bundle fails
  the request without having wiped anything) → truncate `runtime_settings`
  (`SettingsRepository.clear_all`) → `ProfileStateRepository.clear()` →
  `apply_profile(force=True)` → republish profile-derived runtime state via
  the same `_publish_runtime_state` hook auto-bootstrap uses (offer-resolver
  fallback, RD cache, OpenAPI drop, SHACL + anonymous-authz warm-up), so the
  reset takes effect with no restart.
- Returns a report (profile name/version + counts of settings cleared,
  schemas / offers / resource-definitions / seed-records re-applied).
  Audit-logged via structlog (`admin_reset_completed` + `settings_cleared_all`),
  consistent with the settings audit approach.

**Scope note:** "factory defaults" = profile-managed state. The force
re-apply overwrites the profile's graphs in place (matching `fdp profile
apply --force` CLI semantics) and reverts runtime RD edits via the rewritten
seed records; it does **not** blanket-wipe operator-created records — there
is no drop-all-graphs capability in the SPARQL 1.1 adapter (ADR-0005), and a
full triple-store wipe is a heavier, separate concern.

Tests: `tests/unit/metadata/test_admin.py` (9 — `clear_all` against SQLite,
router auth/confirmation gating, happy-path report + subject threading,
no-bundle guard). Full unit suite green (668).

References: `MetadataSchemaService.java`, `ResourceDefinitionService.java`,
`ResetService.java`, `FactoryDefaults.java`.

---

## Phase 11 — Authentication extensions

### 11.1 [ ] API Keys (`POST /me/api-keys`)

The reference impl issues long-lived API keys per user, suitable for
scripts / CI. Our server is OIDC-only today, which makes
machine-to-machine flows harder than they need to be (a client_credentials
grant works but requires per-client Keycloak config).

- Postgres `api_keys(id, owner_subject, label, hash, created_at,
  expires_at, last_used_at, revoked_at)`. Storing the hash, not the key.
- `Authorization: Bearer <api-key>` is accepted by the auth middleware
  as a fallback when the token doesn't validate as a JWT. Successful
  match populates `RequestContext` with the owner's identity.
- Self-service CRUD under `/me/api-keys`. Admins can revoke any key.

### 11.2 [x] Anonymous read auth-cache warming (already TODO in main.py:142)

Pre-populate `authz_index` for the anonymous subject at startup so the
first anonymous read does not pay a cache-miss roundtrip. Currently a
single-line comment in `main.py`; promote to a real task with the
metric "first-anonymous-request latency under N ms".

References: `ApiKeyService.java`, `ApiKeyController.java`.

---

## Phase 12 — Metadata lifecycle (draft/published states)

The reference impl tracks a state per record (`DRAFT | PUBLISHED |
ARCHIVED`) via `MetadataStateService`. The client renders a publish
button and gates visibility per state.

Decided and recorded in [ADR-0010](docs/adr/0010-metadata-publication-state.md):
state lives in the record's meta graph, and visibility is enforced as a
structural gate **layered over** the ODRL decision (in the same slot as the
`is_internal_graph_uri` filter) rather than folded into `authorize()` / the
authz cache — so a transition needs no cache invalidation and the ODRL
evaluator stays a pure function. The ADR's "Alternatives considered" explains
why the literal "thread state into `authorize()`" wording was not taken.

### 12.1 [x] State storage

`fdp:metadataState` ("DRAFT" | "PUBLISHED" | "ARCHIVED") lives in the record's
`<record>/meta` graph (not a Postgres table — it travels with the record).
Delivered:

- `metadata/states.py` (leaf module: the `MetadataState` enum + the
  transition table, depended on by both `meta` and `lifecycle` with no cycle).
- `meta.build_meta_graph` now stamps state: **default `DRAFT`** on create,
  **preserved** across content edits (a `PUT`/`PATCH` never resets it — only the
  transition API does), and an `initial_state` arg the applier passes as
  **`PUBLISHED`** for the root Repository + every seeded record (so the root is
  anonymously readable). Threaded through `MetaWriter.write` →
  `MetadataRepository.put_graph`.
- The default meta-metadata SHACL shape requires the field
  (`sh:in (DRAFT PUBLISHED ARCHIVED)`, `minCount 1`), so every record always
  carries exactly one state. No migration (development re-bootstrap, per the
  scope decision).

### 12.2 [x] State transition API + read gating

`POST /{record}/state` with `{"to": "PUBLISHED"}` — `metadata/lifecycle.py`
(`StateService` + `build_state_router`), mounted before the LDP catch-all
(root `/state` + `/{path}/state`; `state` added to the reserved RD prefixes).
State machine (delivered with the requested **PUBLISHED → DRAFT unpublish**
added):

| From → To | Who |
|---|---|
| `DRAFT → PUBLISHED` | owner (ODRL `modify`) or admin |
| `PUBLISHED → DRAFT` | owner or admin |
| `PUBLISHED → ARCHIVED` | owner or admin |
| `ARCHIVED → DRAFT` | admin only |

Anything else / a same-state no-op → 409; anonymous → 401; non-owner → 403.
"Owner" reuses the PDP (`authorize(ctx, MODIFY, record) == PERMIT`). A
transition writes only the meta graph (swaps the state triple, bumps
`dct:modified`; no content-ETag/version change), emits `RecordStateChanged`,
and is audited (`record_audit.operation = "state_change"`).

**Read gating** — one `StateGate`/`StateReader` consulted at every read PEP:
- LDP `GET`/`HEAD`: after the ODRL read decision, a non-visible record → **404**
  (hides existence). Visible = PUBLISHED, or the caller is admin / holds `modify`.
- SPARQL projection (`StateGate.visible_read_graphs`): the ODRL read set
  intersected with (published ∪ the subject's modify set); anonymous collapses
  to read-and-published. Updates are unaffected.
- `/expanded` + `/page` (draft ancestors/children drop out, not 404 the whole
  response) and the anonymous data provider (a non-published distribution 404s).
- The dashboard surfaces each item's `state`.

Tests: `tests/unit/metadata/test_lifecycle.py` (22 — states table, meta
default/preserve/seed, reader/gate/service/router over a real rdflib `Dataset`
fake) + `tests/integration/metadata/test_metadata_lifecycle.py` (5,
testcontainers Oxigraph + Postgres: seeded-root readable, draft hidden→publish→
archive, anonymous-401, disallowed-409, **SPARQL excludes drafts for anon**).
Full unit suite green (700); integration 5/5.

References: `MetadataStateService.java`, `MetaStateChangeDTO.java`,
[ADR-0010](docs/adr/0010-metadata-publication-state.md).

---

## Phase 13 — Operational endpoints

### 13.1 [x] Build info / app info (`GET /info`)

Spring Boot's `/actuator/info` analogue. Returns build commit, version,
profile name+version, enabled features (matches the `features` block in
Phase 6.4). Unauthenticated. Used by `fdp-client`'s footer.

### 13.2 [x] Liveness vs. readiness split

Today `/healthz` is liveness-only. Add `/readyz` that also checks
the triple store, Postgres, and OIDC discovery reachability. Useful for
Kubernetes deployments and for surfacing dependency outages early in
the client.

References: `AppInfoContributor.java`, Spring Actuator.

---

## Reference-impl features deliberately NOT being ported

| Reference feature | Why we skip it | Reference |
|---|---|---|
| Internal user/password store, password reset, JWT issuance (`UserController`, `TokenController`, `JwtService`, `UserService`) | OIDC is the only auth path. The IdP owns identity. | ADR-0001 |
| Spring ACL membership semantics (`MembershipController`, `MemberController`, `PermissionService`) — per-record `owner/editor/data-provider` role rows | Replaced by ODRL Offers attached to records. ODRL is strictly more expressive but the data model and the client UX both need rewriting against the new model. | ADR-0006 |
| `RepositoryConfig`-style Java config-only triple-store selection | Replaced by `TripleStoreSettings` + capability flags at runtime. | ADR-0005 |
| Mongo-based persistence and Mongo migrations | Postgres-only operational store. | ADR-0003 |
| `IndexFeatureAspect` / annotation-driven feature gates | Use plain settings + capability flags. | ADR-0003 / architecture §5.8 |
| Profile (DCAT-style) `ProfileController` separate from `MetadataSchemaController` | Folded into the single SHACL-shapes pipeline (Phase 10.1). | architecture §10.1 |
| `MetricsMetadataService` (the FDP self-describing as a `dcat:CatalogRecord`) | The meta-metadata graph (`<record>/meta`) plus the dynamic OpenAPI surface cover this. | architecture §6.2 |

If a downstream user genuinely needs one of these, they should raise an
ADR with the justification — none of the omissions are policy
positions, just architectural simplifications we have not seen
counter-evidence against.

---

## Sequencing recommendation

The phases are roughly ordered by client-UX impact and by how much
infrastructure they introduce. A reasonable order for the next
implementer:

1. **Phase 6 first**, in this order: 6.4 (config) → 6.1 (labels) →
   6.3 (dashboard) → 6.2 (autocomplete). Config and labels are tiny
   and immediately visible; dashboard is the next obvious unlock;
   autocomplete depends on the settings work in 9.3 so it lands
   together with that phase.
2. **Phase 7** (search) — biggest single feature, but it stands alone
   and the indexing pipeline composes cleanly with the existing event
   bus.
3. **Phase 9** alongside Phase 6.2 — natural pairing.
4. **Phase 8** (index protocol) — only when you actually want to wire
   this FDP into a registry.
5. **Phase 11.1** (API keys) — when machine-to-machine flows become
   painful.
6. **Phase 10 and 12** — deeper changes, want an ADR per phase before
   starting.
7. **Phase 13** — small, can be slotted anywhere.
