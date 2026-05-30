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

### 6.1 [ ] Labels endpoint (`GET /labels`)

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

### 6.2 [ ] Form autocomplete (`GET /forms/autocomplete`)

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

### 6.3 [ ] User dashboard (`GET /me/dashboard`)

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

### 9.1 [ ] Settings storage and read API

Postgres table `runtime_settings(key, value_json, updated_by, updated_at)`.
`GET /settings` returns the merged view (defaults from the profile,
overlaid with runtime overrides). Public — the client reads it on every
load.

### 9.2 [ ] Settings update API (admin-only)

`PUT /settings/<key>` with a JSON body. Validated against a Pydantic
model per key. Audit-logged.

### 9.3 [ ] Forms-autocomplete sources management

`GET/POST/PUT/DELETE /settings/forms/autocomplete-sources`. Each source
declares its name, type (`inline | sparql | vocabulary-uri`), and the
payload (the list of items, the SPARQL query, or the URI). Feeds Phase
6.2.

### 9.4 [ ] Search filter configuration

`GET/PUT /settings/search/filters` — declare which filter dimensions are
exposed on the search page, their labels, and their facet expansion.

References: `SettingsController.java`, `SettingsService.java`,
`SettingsFormsAutocompleteDTO.java`, `SettingsSearchDTO.java`.

---

## Phase 10 — Schema and ResourceDefinition runtime admin

The reference impl exposes `MetadataSchemaController` (CRUD over SHACL
shapes with versions and drafts) and `ResourceDefinitionController` (CRUD
over the typed-record definitions). Right now the new server treats both
as profile-bundle artefacts — to change them you re-apply the profile.

The trade-off: runtime admin is faster for iterating, but the profile
bundle is what makes a deployment reproducible. A compromise is to
allow runtime *additions* but require the profile to be re-applied to
*remove* anything declared by the active profile.

### 10.1 [ ] Schema admin API

`POST /admin/schemas` to upload a new SHACL shape (Turtle body). Stored
as a versioned IRI in the triple store; the in-memory cache is
invalidated. Drafts are first-class — a shape can be saved as a draft,
previewed against a sample record, then released.

### 10.2 [ ] Remote schema synchronization

A schema can declare `dct:source <remote URL>` and the server
periodically refetches and bumps the version. Disabled by default;
opt-in per-schema.

### 10.3 [ ] ResourceDefinition admin API

`POST /admin/resource-definitions` to add a new typed-record kind at
runtime. Validates that the referenced schema exists and that the
`urlPrefix` doesn't collide. The OpenAPI generator must refresh
(`app.openapi_schema = None`) after a successful add.

### 10.4 [ ] Reset to factory defaults

`POST /admin/reset` — admin-only and destructive. Truncates the
Postgres `runtime_settings` table and re-applies the bundled profile
with force. Confirmation token required in body. Audit-logged.

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

### 11.2 [ ] Anonymous read auth-cache warming (already TODO in main.py:142)

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

### 12.1 [ ] State storage

Add `meta:state` to the meta-metadata graph. Default for new records
created via the LDP layer: `DRAFT`. State is part of the meta-metadata,
not a separate table — it travels with the record.

### 12.2 [ ] State transition API

`POST /<record-iri>/state` with `{to: "PUBLISHED"}`. Validated:
- Only the record's owner (per ODRL Offer) or an admin can transition.
- `DRAFT → PUBLISHED` is allowed; `PUBLISHED → ARCHIVED` is allowed;
  `ARCHIVED → DRAFT` requires admin.
- The PDP read decision must consult the state: anonymous reads
  succeed only for `PUBLISHED`. This is a bigger change — it requires
  threading `state` into the `authorize()` call. Consider a new ADR
  before implementing.

References: `MetadataStateService.java`, `MetaStateChangeDTO.java`.

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
