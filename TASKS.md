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

### 2.2 [x] SHACL validation pipeline
- `metadata/shacl.py` — wraps pySHACL with a fast-path for cached compiled shapes.
- `validate_against(graph, shape_iri)` returning a structured violation report or success.
- Profile bootstrap pre-compiles known shapes; runtime falls back to compile-on-first-use.

`shacl.py` was complete; the remaining work was making **SHACL-on-write
actually enforce** consistently (the cross-cutting concern shared with 2.3/2.4/2.5):

- **Record validation on `PUT`** was a no-op — it resolved the *container's*
  member shape against a leaf member IRI (→ `None`), so a `PUT` could replace a
  record past its type shape. Now `PUT`/`PATCH` validate against the resource's
  **own** type shape (`shape_for`); `POST` still uses the container member shape.
- **Meta-metadata validation is now active at runtime.** The validating
  `MetaWriter` is installed on the repository (`enable_meta_validation`, wired
  post-construction to avoid a build cycle), so every write validates the meta
  graph against `META_SHAPE_IRI` (closes the 2.5 gap). Prerequisite fixed: the
  applier now **stores** the meta shape at `META_SHAPE_IRI` (it was previously
  loaded only for bootstrap-time profile validation, never persisted), and the
  writer **degrades safely** if the shape is absent (a profile without
  `metaMetadataSchema` skips meta validation rather than failing every write).
- Corrected the stale `main.py` comment that claimed SHACL-on-write was off.

Tests: `test_meta.py` (validate-when-present + tolerate-missing), `test_applier.py`
(meta shape stored first), `test_router.py` (PUT validates the resource shape:
422 on violation, 201 on conformance). Full unit suite green (744); the
lifecycle/search/RD integration suites pass with both validations active.

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

#### 6.1a [x] Vocabulary labels via the autocomplete sources (follow-up)

**Done.** `LabelResolver` now takes an optional `settings_repository` and, after
the knowledge-graph SPARQL pass, fills still-missing IRIs from an inline-label
map flattened from the `forms.autocomplete-sources` setting (inline items only;
`iri → label`, TTL-cached ~5 min so admin edits show without restart). The graph
always wins; the inline map fills gaps and is language-neutral. Wired in `main.py`
(settings repo built before the resolver). Verified live: the CC BY 4.0 and MIT
license IRIs resolve to "Creative Commons Attribution 4.0" / "MIT License" while
record IRIs still resolve from the graph. Tests: `test_labels.py` (graph-miss →
inline fill, graph-wins-over-inline, graph-only when no settings repo); full unit
suite green (747). Remote-vocabulary resolution stays the deferred third source.

**Problem (found in client integration).** `GET /labels` resolves only IRIs the
*local graph* describes (record `dct:title` etc.). External vocabulary IRIs —
`dct:license` (CC BY 4.0), themes, publishers — return `{}` because the KG only
*references* them, never *labels* them. Yet the curated
`forms.autocomplete-sources` setting already maps those exact IRIs to nice
labels. The client falls back to a short label, so this is **non-blocking**, but
the intended license/theme/publisher names won't show until fixed.

**Fix (client's option b — reuse curated data; no remote fetch, no migration,
no ADR):** add a secondary label source behind the existing `LabelResolver`
(the module docstring already anticipates "a secondary resolver behind the same
interface").

- `metadata/labels.py`: after the local-KG SPARQL pass, resolve any still-missing
  IRIs from an **inline-autocomplete label map** — flatten every `inline`
  `AutocompleteSource.items` (`forms.autocomplete-sources` setting) into
  `iri → label` (use `label`, ignore `aliases`; skip `sparql`-kind sources). KG
  wins (record/instance labels); inline fills the gaps (vocab labels). Reuse the
  existing per-`(iri, language)` TTL cache; treat inline labels as
  language-neutral (lowest precedence).
- Inject the source: pass `LabelResolver` a `SettingsRepository` (or a small
  `inline_labels_provider()` reading `read_with_default("forms.autocomplete-sources")`)
  and wire it in `main.py`. Read with a short TTL so admin edits to the sources
  show up without restart.
- Tests: a license IRI present in the inline source but absent from the KG →
  resolves to its curated label; a record IRI → still from the KG; an IRI in
  neither → omitted.
- Leaves the deferred **remote-vocabulary** resolver (allow-listed outbound) as a
  future *third* source behind the same interface (architecture §10 note in
  `labels.py`).

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

All three sub-tasks delivered in `src/fdp/metadata/search/`. No new ADR — the
result-visibility gate reuses ADR-0010 (publication state) rather than
inventing one. Migration `0008` adds both tables.

### 7.1 [x] Search index and ingestion pipeline

- `metadata_search` (migration `0008`): one row per indexable record —
  `(record_iri, type_iri, title, description, license, keywords,
  search_text tsvector, state, anon_read, updated_at, language)` with a GIN
  index on `search_text`. The two **visibility flags** (`state`, `anon_read`,
  computed once at index time) are what gate anonymous search cheaply — the
  hybrid model chosen in planning, not a bare authorized-set join.
- `search/extract.py` — pure record-graph → fields; `search/repository.py` —
  the Postgres FTS write (`to_tsvector`) + ranked query; `search/indexer.py` —
  event subscriber (`RecordCreated`/`RecordModified`/`RecordStateChanged` →
  upsert, `RecordDeleted` → delete), skipping internal/config records.
- `fdp search reindex` CLI walks every non-internal graph and rebuilds the
  index (and re-derives `anon_read`, repairing inherited-offer drift).

### 7.2 [x] Search query API

- `POST /search` `{query, types, license, from/to, offset, limit, language}` →
  `{items, total, facets}`. FTS via `plainto_tsquery` + `ts_rank`; filters by
  type / license / date; all SQLAlchemy-parameterised (no interpolation).
- **Visibility gate (ADR-0010):** anonymous callers get only
  `state='PUBLISHED' AND anon_read`; authenticated callers also see
  `record_iri = ANY(StateGate.visible_read_graphs(ctx))` — their drafts +
  private records they can read. So search respects ODRL **and** publication
  state, and anonymous discovery is fast/complete without warming.
- **Facets driven by the 9.4 `search.filters` settings** (advancing 9.4):
  configured filter predicates select the exposed dimensions + labels, with
  type/license as built-in defaults.

### 7.3 [x] Saved queries

- `search_saved_queries` + `SavedQueryService` + `/me/saved-queries` CRUD
  (`GET`/`POST`/`PUT`/`DELETE`). Owner-scoped; the stored `query` is validated
  as a runnable `SearchRequest`. The `shared` toggle is **admin-only**; shared
  queries appear in everyone's list.

**Side fix (latent LDP-layer bug surfaced by search):** the LDP `PUT`/`POST`
body was parsed with no base, so a relative `<>` ("this resource") resolved to
an rdflib-invented `file://` subject instead of the record IRI — invisible to
any subject-keyed read (search, dashboard titles, `/expanded`). `negotiation.parse`
now takes a `base`; `PUT` passes the target IRI and `POST` mints the member IRI
*before* parsing. Fixes the storage so `<>` records carry their real subject.

Tests: unit `tests/unit/metadata/search/` (22 — extract, indexer dispatch,
service gating/facets, saved-queries repo+router); integration
`tests/integration/metadata/search/` (8 — Postgres FTS query/filter/facet/gating
+ an Oxigraph+Postgres end-to-end create→draft-hidden→publish→searchable). Full
unit suite green (741).

References: `SearchService.java`, `SearchSavedQueryService.java`,
`SearchFilterCache.java`.

---

## Phase 8 — Index protocol (cross-FDP discovery)

> **Rescoped 2026-07-05 per ADR-0020/0021.** The index is no longer a
> mode of this server: it is a separate product, **FAIR Discovery**
> (`discovery` distribution), composed of the new `registry/` and
> `harvest/` contexts. The old question "is THIS server (a) an Index,
> (b) an FDP that pings, or (c) both via `INDEX_FEATURE_ENABLED`?" is
> answered by composition: the **FDPneo distribution implements only the
> outbound side** (8.1 below); the intake, entries, harvesting, admin,
> and webhooks (formerly 8.1–8.2, 8.4–8.5) move to the FAIR Discovery
> backlog — see `docs/adr/0021-fair-discovery-product.md` and
> `docs/architecture/discovery.md`.

The wire protocol stays compatible with the reference implementation —
FDPs ping an index with their URL, the index harvests their metadata —
so a FDPneo deployment remains discoverable by the community indexes
(e.g. `https://home.fairdatapoint.org`) and by FAIR Discovery
deployments alike. "Index" remains the protocol vocabulary; FAIR
Discovery is the product name (ADR-0021 §1).

### 8.1 [x] Outbound Ping service (FDPneo side — the only Phase 8 work in this distribution)

_Done: `metadata/index_ping.py` (`IndexPinger` + `ping_indexes`) — startup + periodic (`FDP_INDEX_PING_INTERVAL_SECONDS`, default 7d) + throttled on-publish pings; `fdp index ping` CLI for external cron. Reference wire protocol verified against `IndexPingController` (`POST {index}/` `{clientUrl}`, 204). Settings `FDP_INDEX_PING_TARGETS` / `_INTERVAL_SECONDS`._

A scheduled job (arq or the existing metrics rollup pattern) POSTs
`{clientUrl: settings.base_url}` to every configured index URL on a
configurable interval (default 7 days), and additionally on publish
events so indexes can harvest changes promptly (ping-on-change powers
FAIR Discovery's incremental harvesting).
Settings: `FDP_INDEX_PING_TARGETS=https://a.example,https://b.example`,
`FDP_INDEX_PING_INTERVAL_SECONDS=604800`.

### 8.2 [moved] Intake side → FAIR Discovery

Index entries model and state machine, incoming ping endpoint
(`POST /index/ping`, unauthenticated, rate-limited), harvester, index
admin API, and state-change webhooks are `registry/` and `harvest/`
context work in the `discovery` distribution. Tracked in the FAIR
Discovery design doc (`docs/architecture/discovery.md`), not here.

References: `IndexEntryService.java`, `EventService.java`,
`HarvesterService.java`, `WebhookService.java`, `IncomingPingUtils.java`
(reference impl, for wire compatibility).

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

### 9.4 [x] Search filter configuration

Declare which filter dimensions are exposed on the search page, their labels,
and their facet expansion. The `search.filters` data shape
(`SearchFilter`/`SearchFilters` in `metadata/settings.py`) is CRUD-able through
the generic runtime-settings surface (`GET /settings/search.filters`,
`PUT /settings/search.filters`, admin-gated) — the established pattern for every
settings key, rather than a bespoke `/settings/search/filters` path. As of
Phase 7.2 it is **consumed**: `SearchService` reads `search.filters` to decide
which facet dimensions to expose and their labels (type/license built in),
so an admin can reconfigure search facets at runtime without a redeploy.

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

### 10.5 [x] Profile schemas are first-class editable schemas

**Problem.** When a profile is applied, its SHACL schemas should — from that
point on — behave exactly like a runtime/user-published schema (listable in
`GET /schemas`, editable via `PUT /schemas/{id}`, deletable via `DELETE`),
with the sole exception of the **FDP root schema** (the root resource
definition's schema, `fdp:Repository` in the default profile), which is
**editable but not deletable**. Today they are neither listable nor editable:
the client cannot see the default DCAT schemas in the Schema-list UI and
cannot edit them.

**Root cause.** Profile schemas and runtime schemas live in two different
namespaces. The applier stores each profile schema at its **vocabulary IRI**
(`expander.schema_iri("dcat:Catalog")` → `http://www.w3.org/ns/dcat#Catalog`,
[`applier.py`](src/fdp/metadata/profiles/applier.py) step 1), and the root RD
references it via `ldp:constrainedBy`
([`rd_records.py`](src/fdp/metadata/profiles/rd_records.py)). The schema-admin
API ([`schemas.py`](src/fdp/metadata/schemas.py)) only ever manages the
reserved `{base}/fdp-api/schemas/{slug}` namespace — `list_schemas` filters
`STRSTARTS(?g, "{schema_namespace}/")` and `GET/PUT/DELETE /schemas/{id}`
resolve through `schema_graph_uri(base, id)` — so profile schemas are invisible
to the list and 404 on edit/delete. `SEED_STATE` is **not** the blocker;
it is purely the namespace split.

**Chosen approach — migrate profile schemas into the schemas namespace.**
Store profile schemas at `{base}/fdp-api/schemas/{slug}` like user schemas, and
point RD `ldp:constrainedBy` at that storage IRI. The shape node and its
`sh:targetClass` inside the graph stay the class IRI (`dcat:Catalog`), so
pySHACL matching against record `rdf:type` is unchanged — only the *graph IRI*
(the storage/fetch key) moves. Server-owned machinery shapes (meta-metadata
`META_SHAPE_IRI`, license `LICENSE_SHAPE_IRI`, RD `RD_SHAPE_IRI`) stay at their
fixed IRIs and are deliberately **not** exposed as user-editable schemas.

Sub-tasks:

- **Slug derivation.** Add a shared, deterministic `schema_slug(curie)` helper
  (kebab-cased local name: `dcat:DataService` → `data-service`,
  `fdp:Repository` → `repository`) used by *both* the schema-write step and the
  RD-build step so a schema and the RD that references it resolve to the same
  `{base}/fdp-api/schemas/{slug}` IRI. Enforce slug uniqueness across the
  profile in `validate_profile` (collision → structural validation error).
- **Applier change.** In `apply_profile` step 1, write each `profile.schemas`
  entry to `schema_graph_uri(base, schema_slug(id))` instead of
  `expander.schema_iri(id)`. Keep `initial_state=SEED_STATE`. Track the written
  IRIs in `ApplyReport.schemas_written` as today.
- **RD reference rewrite.** Make the registry's schema-IRI resolution
  (`records_from_manifest` / `build_cache_from_manifest`, via `IRIExpander`)
  map a declared-schema CURIE to its `{base}/fdp-api/schemas/{slug}` IRI rather
  than the class IRI, so `ResourceDefinition.schema_iri` (→ `ldp:constrainedBy`)
  and the in-memory RD cache both point at the new location. A CURIE that is
  *not* a declared profile schema keeps falling back to the expanded class IRI
  (or errors — decide and test). `resolve_runtime_state` (startup re-derive,
  profile already applied) must produce the **same** IRIs.
- **Validator/shape-provider.** No code change expected — `MetadataShapeProvider`
  fetches whatever IRI the RD's `constrainedBy` names. Add a test asserting a
  record validates against its profile schema at the new IRI, and that
  `PredefinedShapeProvider` still answers the server-owned fixed IRIs.
- **FDP-root protection (editable, not deletable).** Teach `SchemaService` the
  root schema IRI (derive from the RD cache root: `rd_cache.root().schema_iri`;
  inject at bootstrap and on every profile (re)apply / RD mutation). In
  `delete()`, hard-refuse the root schema with a `Forbidden` carrying a stable
  code (e.g. `fdp.schema_protected`) — distinct from the existing
  `_is_referenced` 409. `put()` on the root stays allowed. (Note: non-root
  profile schemas remain subject to the existing `ldp:constrainedBy`
  delete-guard — they can only be deleted after their RD is removed/repointed,
  same as user schemas; that is intended, not part of this task.)
- **List response flag.** Add `deletable: bool` (and/or `protected: bool`) to
  `SchemaInfo` so the client can render the lock and hide the delete action on
  the FDP root schema. Populate from the root-schema-IRI check.
- **Data migration for already-applied deployments.** Schemas live in the
  triple store (no Alembic). Provide a one-shot reconciliation (a new
  `fdp schema migrate-namespace` CLI command, or an idempotent startup
  reconcile): for each profile schema currently at a vocabulary IRI, copy the
  graph to `{base}/fdp-api/schemas/{slug}`, rewrite every RD `ldp:constrainedBy`
  pointing at the old IRI, drop the old graph + its `/meta` sibling, and
  invalidate the validator cache. Idempotent (skip if already migrated). Decide
  whether dev deployments simply `force-apply` instead and document the choice.
- **SPARQL projection check.** Profile schemas moving under `/fdp-api/schemas/`
  are now recognised by `is_schema_graph_uri` and treated like policies/licenses
  (public reference docs, anonymous-readable, not internal). Verify
  `PDP.authorized_graphs` and the public-dataset projection behave correctly
  (a schema previously surfaced at a vocab IRI in the public KG should not
  silently disappear from any expected query) — add/extend a projection test.
- **OpenAPI + client coordination.** The `SchemaInfo` change updates
  `/openapi.json`; regenerate types in `fdp-client` and surface profile schemas
  (with the FDP-root lock) in the Schema-list UI. Cross-repo follow-up.

Tests: unit — slug derivation + uniqueness validation; applier stores schemas
in the schemas namespace; RD `constrainedBy` resolves to the new IRI;
`resolve_runtime_state` parity; root-schema `DELETE` → 403, `PUT` → ok; non-root
`DELETE` after RD removal → ok; `list_schemas` includes profile schemas with the
`deletable` flag. Integration — apply default profile over GraphDB/Oxigraph,
`GET /schemas` lists all five DCAT schemas (four deletable + the protected
root), edit `dcat:Catalog`, a Catalog record still validates, `DELETE` the root
schema → 403, run the migration against a pre-existing vocab-IRI deployment and
re-assert the above.

References: architecture §10.1, §12.2 (profile bootstrap),
[ADR-0009](docs/adr/0009-runtime-resource-definitions.md) (RD ↔ schema
references), `metadata/profiles/applier.py`, `metadata/profiles/registry.py`,
`metadata/profiles/iri.py`, `metadata/schemas.py`, `shared/graphs.py`.

**Delivered (server-side):**

- `schema_slug()` + `IRIExpander.schema_storage_iri()` in
  `metadata/profiles/iri.py` (kebab-cased local name → `{base}/fdp-api/schemas/{slug}`),
  with a `duplicate_schema_slug` structural check in `validate_profile`.
- Applier writes profile schemas to the storage IRI; the registry points RD
  `ldp:constrainedBy` there (the `relationUri` predicate stays the class IRI).
  The shape node + `sh:targetClass` are unchanged, so SHACL matching is intact.
- `SchemaService` learns the FDP root schema IRI via a provider
  (`main._root_schema_iri`, read from the live RD cache): `DELETE` of the root
  raises `fdp.schema_protected` (403); `PUT` stays allowed; `SchemaInfo` gained
  a `deletable` flag.
- Non-destructive, idempotent reconciliation `migrate_schema_namespace`
  (`metadata/profiles/migrate.py`) + `fdp schema migrate-namespace <bundle>` CLI
  for pre-10.5 deployments.
- Tests: `test_iri.py`, `test_migrate.py`, slug-collision in `test_validator.py`,
  registry/applier assertions updated, root-protection + `deletable` cases in
  `test_schemas.py`, and an end-to-end integration test
  `tests/integration/metadata/test_schema_namespace.py` (testcontainers Oxigraph
  + Postgres): apply → `GET /fdp-api/schemas` lists both profile schemas (root
  `deletable:false`, other `true`), the relocated shape is fetchable, root
  `DELETE` → 403 `fdp.schema_protected` while `PUT` → 200, non-root `DELETE` →
  409 (RD reference guard). Unit suite green (907) + integration (2/2); ruff +
  pyright clean.
- Live-verified on the GraphDB dev stack: migrated the running deployment (5
  schemas moved, 5 RDs repointed), `GET /fdp-api/schemas` lists all five
  (root `deletable:false`, others `true`), the relocated shape is fetchable and
  resolves for validation, and the old vocabulary IRI graph is emptied. Wiring
  the RD cache at startup needs `FDP_PROFILE_AUTO_APPLY=true` (added to dev
  `.env`); without it `app.state.resource_definitions` is `None` and the
  root-protection provider can't engage.

**Client (cross-repo): done.** `fdp-client` regenerated its API types for the
`deletable` field and now surfaces profile schemas in the Schema-list UI with
the root schema locked (delete disabled, `fdp.schema_protected` 403 handled).
Server + client are aligned; 10.5 is fully closed.

---

## Phase 11 — Authentication extensions

### 11.1 [x] API Keys (`POST /me/api-keys`)

The reference impl issues long-lived API keys per user, suitable for
scripts / CI. Our server is OIDC-only today, which makes
machine-to-machine flows harder than they need to be (a client_credentials
grant works but requires per-client Keycloak config).

Delivered per [ADR-0011](docs/adr/0011-api-keys.md) — a key is an *alternate
credential for an IdP-owned identity*, not a new identity (keeps ADR-0001
intact). `src/fdp/identity/api_keys.py` (model + repository + `ApiKeyService` +
`/me/api-keys` router) and migration `0007`:

- **Token + storage**: `fdpk_` + ~190-bit random, shown **once**; only
  `sha256(token)` is stored (fast hash is correct for high-entropy keys).
  Postgres `api_keys(id, owner_subject, label, key_hash, display_prefix,
  roles_json, groups_json, created_at, expires_at, last_used_at, revoked_at)`.
- **Middleware**: an `Authorization: Bearer fdpk_…` is dispatched by prefix to
  the key authenticator (Postgres lookup); everything else stays the JWT path
  (kept DB-free). An unknown/revoked/expired/disabled key → 401, like a bad JWT.
- **Live roles, not a frozen grant** (the refinement from the planning Q&A):
  a new `subject_principal` table records each subject's freshest IdP roles,
  upserted (throttled) by the middleware on every JWT login. API-key auth
  resolves roles from there (mint-time snapshot is only a seed/fallback), so a
  long-lived key tracks the owner's current roles — a role change reflects on
  the owner's next interactive login, and the PDP evaluates live per request.
  Per-key revoke (owner **or admin**) is the immediate kill switch. Known
  bound (documented in ADR-0011): a *pure* service account that never logs in
  interactively refreshes only via the deferred IdP-sync (§15) or re-mint;
  `subject_principal` is the seam that work plugs into.
- **CRUD** under `/me/api-keys`: `POST` (mint; plaintext returned once),
  `GET` (own keys, secret-free), `DELETE /{id}` (owner or admin). Bounded by
  `FDP_API_KEYS_{ENABLED,MAX_PER_USER,MAX_TTL_DAYS}`; no forced expiry.

Tests: `tests/unit/identity/test_api_keys.py` (14) +
`tests/unit/identity/test_middleware_api_keys.py` (prefix dispatch + throttled
principal recording + role-change-records-immediately) — full unit suite green
(719). Integration `tests/integration/identity/test_api_keys.py` (testcontainers
Postgres + Oxigraph, **no** `current_context` override so the real middleware
runs): a seeded key authenticates as its owner, gains admin live the moment its
`subject_principal` is updated (403→200 on the admin dashboard view), and is
401 after revoke.

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

## Phase 14 — First-class ODRL policy and license documents

Make ODRL access conditions and licenses **managed, reusable RDF documents**
— symmetric with how SHACL schemas were made first-class in Phase 10.1 —
so the client's visual ODRL editor has a real backend and an FDP can act
as a **reference source** of access conditions other FDPs discover and
reference. **Two separate subsystems** (per ADR-0012): `/policies`
(`odrl:Offer`, profile-validated, **PDP-enforced** via `dct:rights`) and
`/licenses` (license documents, validated against a license shape,
**descriptive** via `dct:license`, never enforced).

Read first: [ADR-0012](docs/adr/0012-first-class-odrl-policy-and-license-documents.md),
ADR-0006 (the ODRL profile), ADR-0009 (the runtime-RDF-config model this
mirrors), ADR-0010 (the lifecycle it reuses), Phase 10.1 (`/schemas` — the
parallel admin surface).

**Status (2026-06-06):** built and green (787 unit + 2 integration tests). Done:
14.1, 14.2, 14.3 (incl. the license SHACL shape), 14.4, 14.5, 14.6, and 14.8's
server half (OpenAPI + unit + integration round-trip). Remaining: 14.7 harvest +
remote seam (harvest blocked on Phase 8); 14.8 client `dct:rights`/`dct:license`
pickers (client repo).

### 14.1 [x] Reserved storage namespaces + managed-document plumbing

Reserve deployment-relative namespaces `{base}/policies/{id}` and
`{base}/licenses/{id}` (mirroring `{base}/schemas/{id}`). Store each as
one-graph-per-record (ADR-0007) with public-readable body + internal
`…/meta` and `…/audit` siblings (extend `is_internal_graph_uri` for the
new siblings only — the document graphs themselves are anonymous-readable
reference docs, like schemas). Add stable-IRI minting + `owl:versionInfo`
bump-on-edit helpers (reuse the schema-versioning path).

Done: `policy_graph_uri` / `license_graph_uri` / `is_policy_graph_uri` /
`is_license_graph_uri` in `shared/graphs.py` (doc graphs public; meta/audit
siblings already internal via suffix). Versioning is inherited from
`repository.put_graph` (`owl:versionInfo` bump), as for schemas.

### 14.2 [x] `PolicyService` + `/policies` admin API

Mirror `SchemaService`/`/schemas`. `GET /policies` (catalog, public),
`GET /policies/{id}` (public, content-negotiated, dereferenceable),
`PUT /policies/{id}` (admin; **validate against the FDP ODRL profile** via
`policy/parser.py`, reject out-of-profile with the structured violation
envelope), `POST /policies/{id}/validate` (admin dry-run),
`DELETE /policies/{id}` (admin; **409 if any record references it via
`dct:rights`**). Each policy record carries descriptive metadata
(`dct:title`, `dct:description`, keywords, `odrl:profile`) + meta sibling.

Done: `metadata/policies.py` + `tests/unit/metadata/test_policies.py`. The
Offer subject is the stable IRI (body may use a relative `<>`).

### 14.3 [x] `LicenseService` + `/licenses` admin API

Same shape as 14.2 but for license documents (`odrl:Set`/`odrl:Policy`
license expression or `dct:LicenseDocument`). Delete refused with 409 if
referenced via `dct:license`.

Done: `metadata/licenses.py` + `tests/unit/metadata/test_licenses.py`; the
**default license set** (CC0, CC BY 4.0, CC BY-SA 4.0) is seeded by 14.5.
Validation is **SHACL against the server-owned license shape**
(`LICENSE_SHAPE_IRI = fdp:LicenseDocumentShape`): a managed license must carry a
`dct:title` (and an IRI `dct:source` if present) at its stable IRI. The shape
targets a synthetic `fdp:ManagedLicense` type injected at validation time
(`_probe_graph`), so the contract holds whatever the document's own `rdf:type`
is. The shape resolves from **code** via `PredefinedShapeProvider` (composite in
front of the store-backed `MetadataShapeProvider`), so license validation works
even on a deployment whose profile was applied before the shape existed — it no
longer depends on the seeded copy. (Client-reported fix: `PUT`/`validate
/licenses` had 500'd with `UnknownShapeError` on already-applied deployments.)

### 14.4 [x] Lifecycle: reuse Phase 12 publication state

Policies/licenses use the existing `DRAFT → PUBLISHED → ARCHIVED` state
machine (`POST /{record}/state`). Special rule for **archived policies**:
retained and **still resolvable/enforced for records that already
reference them** (archiving must not break dependents), but not offered
for new assignment. Draft policies/licenses are excluded from assignment
pickers and from anonymous discovery.

Done:

- **Archived-still-enforced is inherent + now tested.** Policy/license records
  are ordinary records, so the state router/state-gate apply; the offer resolver
  fetches a policy graph by IRI *regardless of publication state* (the StateGate
  gates record *read* visibility, not offer resolution). `test_resolver.py`'s
  `test_archived_policy_still_enforces_for_existing_reference` proves an archived
  policy keeps enforcing — and asserts the resolver never touches the policy's
  `/meta` state graph.
- **Drafts excluded from discovery / assignment.** `list_policies` /
  `list_licenses` gained a `published_only` filter (joins `fdp:metadataState`
  from the `/meta` graph); the `GET /policies` and `GET /licenses` catalogs pass
  `published_only = caller-is-not-admin`, so anonymous/non-admin callers see only
  PUBLISHED docs while admins see drafts + archived to manage them. `PolicyInfo`
  /`LicenseInfo` now carry `state`. Seeded offers/licenses are `PUBLISHED`
  (SEED_STATE); a fresh `PUT` defaults to `DRAFT`, so it stays out of the picker
  until published. Tests cover both the service filter and the router gating.

Note: individual `GET /policies/{id}` / `GET /licenses/{id}` stay dereferenceable
in any state (the editor loads drafts by id; reference config is low-sensitivity)
— only the *discovery catalogs* are state-gated, which is what "discovery" means.

### 14.5 [x] Profile seeding + PDP wiring

Migrate bundled-profile `offers:` to **seeded managed policies** at
`{base}/policies/{id}`. Point the **system-default offer** at a managed
policy IRI. The `GraphBackedOfferResolver` already resolves offers by IRI,
so the common (local) path needs no change. Add a **synchronous PDP
auth-cache invalidation hook on policy write**.

Done:

- **PDP-cache invalidation hook** — `CacheRepository.invalidate_all` →
  `PDP.invalidate_all` → `RequestScopedPDP.invalidate_all`, called from
  `PolicyService.put`/`delete` (over-invalidates, re-warms lazily; correct
  under `dct:isPartOf` inheritance).
- **Offer → managed policy seeding** — `applier.py` now rewrites each bundled
  Offer's subject from its intrinsic IRI to the deployment-local
  `{base}/policies/{id}` (`_rewrite_subject` + `_managed_policy_iri`) and stores
  it there, so it is dereferenceable and the resolver parses it. Both
  `apply_profile` and `resolve_runtime_state` (restart path) point the
  system-default at the managed IRI.
- **Default license set + license shape** — `metadata/profiles/licenses.py`
  seeds CC0 / CC BY 4.0 / CC BY-SA 4.0 at `{base}/licenses/{id}` (local
  `dct:LicenseDocument` + `dct:source` → canonical CC IRI), and the applier seeds
  the server-owned `LICENSE_SHAPE_IRI` so `PUT /licenses` validates against it.
  Both tracked for rollback; the seeded set is tested to conform to the shape.

The applier writes through `repository.put_graph` (not `PolicyService`) because
it runs at bootstrap before the app's PDP/event-bus are wired; offers are
already profile-validated at load. Tests: `test_applier.py` updated +
`test_default_seeding.py`.

### 14.6 [x] Search + discovery (Phase 7 integration)

Index managed policies and licenses as searchable content. Narrow the
indexer's current blanket `odrl:Offer` skip so it excludes only
**un-managed/seed** offers and the internal siblings, **not** documents under
`…/policies/`. `GET /policies` / `GET /licenses` are the discovery catalogs.

Done: `extract.is_indexable` now includes `/policies/` and `/licenses/` docs
(facet key is the doc's `rdf:type`); `PolicyService`/`LicenseService` publish
`RecordModified`/`RecordDeleted` on the event bus so writes flow live to the
indexer + audit log. `fdp search reindex` picks them up via the same seam.

### 14.7 [ ] Cross-FDP reuse — publish side now, remote enforce later

Deliver the **publishing** capability: stable dereferenceable IRIs +
discovery catalogs + search, so another FDP can find a condition and
reference its IRI. When FAIR Discovery ships (ADR-0021), its harvest can
include the policy/license catalogs. **Defer** actively dereferencing
and enforcing a *remote* FDP's policy at decision time — when added it is
opt-in + allow-listed (same posture as remote schema sync 10.2). Leave a
clearly-marked extension point in the resolver; do not implement the
outbound fetch now.

Done: dereferenceable IRIs (`GET /policies/{id}` / `GET /licenses/{id}`) +
discovery catalogs + search. **Remaining (mostly blocked on Phase 8):** harvest
inclusion; a marked remote-resolution extension point in
`GraphBackedOfferResolver`.

### 14.8 [~] OpenAPI, tests, client coordination

Add `/policies` and `/licenses` to the OpenAPI surface. Unit tests for both
services (validation accept/reject, lifecycle, delete-guard,
archived-still-enforced); integration test for a full author → validate →
publish → reference → enforce round-trip. Coordinate with the client: the
ODRL editor (client Phase 5) now targets `/policies`; add license
management + `dct:rights`/`dct:license` pickers that read the published
catalogs. Note the contract change in the client `TASKS.md`.

Done (server side):

- OpenAPI surface is automatic (routers registered before the LDP catch-all);
  unit tests for both services (validation, delete-guard, lifecycle events).
- **Integration round-trip** — `tests/integration/metadata/test_policies_licenses_e2e.py`
  drives the whole stack over Oxigraph + Postgres: author → validate (incl. a
  422 out-of-profile reject) → publish → anonymous discovery excludes drafts →
  reference via `dct:rights`/`dct:license` → **enforce** (a steward-only policy
  hides a record from anonymous while an open policy exposes it, isolating the
  policy as the cause) → delete-guard 409. The license half exercises SHACL
  validation + the seeded default set.
- **Bug fix surfaced by the round-trip:** `TripleStoreAdapter.drop_graph` now
  tolerates a Graph-Store-Protocol 404 (absent graph), matching the
  `DROP SILENT` fallback — a managed schema/policy/license has no audit sibling,
  so deleting one would otherwise 404 the whole delete. Unit test added.
- Client `TASKS.md` updated (Phase 5 unblock note).

**Remaining:** the client `dct:rights`/`dct:license` pickers + license-management
views (client repo, not this one).

---

## Phase 15 — v0.2 release: LDP conformance + DCAT v3 modular schemas

Two release-gating items: prove (and complete) the LDP server, and rebuild the
default profile's schemas from the full DCAT 3 + FDP-O vocabularies as composed,
modular SHACL shapes.

### 15.1 [x] LDP conformance: true Direct Containers + a conformance suite

> **Started — root is a real Direct Container.** `applier.direct_container_config`
> derives the membership triad from a container's RD child relations
> (`ldp:DirectContainer` + `ldp:membershipResource` = self + one
> `ldp:hasMemberRelation` per child link + `ldp:insertedContentRelation
> ldp:MemberSubject`); the root seed now emits it (no more `ldp:BasicContainer`).
> The LDP `Link: rel=type` header is fixed (containers advertise `ldp:Container`
> **and** `ldp:DirectContainer`; `Accept-Post` only on containers) and GET/HEAD
> now advertise the shape via `Link: rel="…ldp#constrainedBy"`. Tests:
> `test_applier.test_direct_container_config…`, integration asserts the live root
> is a Direct Container with the membership config + Link headers.
>
> **Runtime container records too.** `ResourceDefinitionCache.member_relations`
> returns a record's RD child relations; the LDP router stamps
> `direct_container_config` onto any container-type record on create (PUT/POST),
> so created Catalogs/Datasets are genuine Direct Containers (a leaf like
> Distribution gets nothing). Tests: `test_registry.test_member_relations…`,
> integration asserts a created Catalog carries `ldp:DirectContainer` +
> `ldp:hasMemberRelation dcat:dataset/dcat:service`.
>
> **Conformance suite started.** `tests/conformance/test_ldp.py` (testcontainers,
> real default profile) codifies the LDP MUST behaviors: the `Link: rel=type`
> interaction model (Resource/RDFSource/Container/DirectContainer) +
> `constrainedBy`, `Allow`/`Accept-Post`/`Accept-Patch`, ETag, content
> negotiation incl. 406, 415 on bad Content-Type, and Direct-Container
> membership — **all 7 pass on Oxigraph**, including the PUT create / If-Match
> concurrency / DELETE round-trip and the 405-on-POST-to-leaf check.
>
> **Resolved (2026-06-12): the "Oxigraph first-write 500" was a test-fixture bug,
> not an Oxigraph non-conformance.** The two write checks were `xfail`ed on the
> theory that Oxigraph's SPARQL protocol mishandled writes. Root-causing it showed
> the opposite: the conformance fixture authored as `RequestContext(subject="u#a")`
> — a *relative* IRI — which flows through to `dct:creator <u#a>` in the meta
> graph. Oxigraph's strict (spec-correct) N-Triples parser rejects the
> scheme-less IRI with `400 No scheme found in an absolute IRI`; GraphDB merely
> tolerates it. Production is unaffected: `identity/middleware.py` always mints
> the subject as the absolute `{issuer}#{sub}`. Fix: the fixture now uses an
> absolute subject and both `xfail`s are removed, so the suite fully asserts LDP
> writes on Oxigraph. (Note: this is *unrelated* to the genuine Oxigraph
> repeated-`named-graph-uri` multi-graph *read* under-projection documented in
> 10.3 — that finding stands; it concerns `/sparql` projection, not writes.)
>
> **Pre-15.1 membership backfill — DONE (2026-06-12).** Deployments bootstrapped
> before containers became genuine `ldp:DirectContainer`s still hold them as
> `ldp:BasicContainer` with no membership triad. `metadata/profiles/backfill.py`
> (`backfill_direct_container_membership`) + the `fdp ldp backfill-membership` CLI
> reconcile them non-destructively: walk every non-internal record graph, resolve
> each to its RD (via `build_cache_from_repository`), and for any container (a
> type whose RD declares child links) add the membership config and strip the
> stale `ldp:BasicContainer` type. The rewrite goes through `adapter.replace_graph`
> (not `repository.put_graph`) on purpose — stamping LDP affordance triples is a
> structural fix, not a content edit, so it must not bump `owl:versionInfo` /
> `dct:modified`. Idempotent (an already-conformant container is skipped); leaves
> leaf records and internal siblings untouched. Tests: unit
> `tests/unit/metadata/profiles/test_backfill.py` (stamp + strip + leaf/sibling
> untouched, idempotency) and integration
> `tests/integration/metadata/test_membership_backfill.py` (apply profile over
> Oxigraph → downgrade the seeded root to its pre-15.1 shape → backfill restores
> the Direct Container config in place → second pass is a no-op).
>
> **Conformance matrix + ADR-0008 rewrite — DONE (2026-06-12), closing 15.1.**
> [`docs/conformance/ldp-conformance.md`](docs/conformance/ldp-conformance.md)
> records the conformance position requirement-by-requirement (LDPR §4 + LDPC §5
> tables, MUST/SHOULD/MAY × Conformant/Partial/Deviation/Gap), naming the
> `test_ldp.py` function that exercises each load-bearing row, the conformance
> classes claimed (LDPR, LDP-RS, LDP-DC — not Basic/Indirect/Non-RDF), the
> deliberate deviations (LD-PATCH, `X-FDP-Page-*` paging, no LDP-NR) with their
> ADRs, and the open SHOULD/MAY gaps (`Allow` on 4xx, `Prefer` minimisation,
> external `ldp-testsuite` run). [ADR-0008](docs/adr/0008-full-ldp-with-patch.md)'s
> implementation-status section was rewritten to match shipped reality (runtime
> stamping + backfill done) and points at the matrix.
>
> **Conformance gaps closed (2026-06-15).** The two code-level SHOULD gaps are now
> implemented + tested: (1) advisory headers on 4xx — `FDPError` carries optional
> headers (emitted by the handler and the catch-all middleware), so a 405 carries
> `Allow` (RFC 7231 MUST), a container-POST 415 carries `Accept-Post`, and a PATCH
> 415 carries `Accept-Patch`; (2) `Prefer` container minimisation — a container
> `GET` honours `omit`/`include` of `ldp:PreferContainment`/`PreferMembership`/
> `PreferMinimalContainer`, returns `Preference-Applied`, and advertises
> `Vary: Prefer`. The in-repo conformance suite now runs as a dedicated
> **`conformance` CI job** gating the image build. Tests: `tests/unit/metadata/ldp/test_router.py`
> (+6) and `tests/conformance/test_ldp.py` (Allow assertion + `test_container_prefer_minimisation`).
> Only remaining (MAY, optional): running the *external* W3C Java `ldp-testsuite`.
> (Deliberate deviations stand: custom `X-FDP-Page-*` paging, SPARQL-Update PATCH
> only.)

**Decision:** implement real LDP **Direct Containers** (not "Basic + typed
relations"). ADR-0008 claims full LDP-DC but the implementation is a Basic
Container that grew typed DCAT relations via the containment work
(`ldp:contains` + e.g. `dcat:catalog`); the `Link: rel=type` header even
advertises `ldp:DirectContainer` while the seed graph is `ldp:BasicContainer`.
v0.2 closes that gap.

Scope:

- **Direct-Container membership on every FDP container.** A container's graph
  declares, per child type, the LDP membership triad:
  `ldp:membershipResource <container>`, `ldp:hasMemberRelation <relation>` (the
  RD child link's `relationUri`, e.g. `dcat:catalog`), and
  `ldp:insertedContentRelation ldp:MemberSubject`. A container with several
  child types (Catalog → dataset + data-service) needs one membership config per
  relation — confirm LDP allows multiple `hasMemberRelation` on one container, or
  model the membership config as repeated blank-node descriptions. The
  `ContainmentManager` writes the membership triple alongside `ldp:contains` on
  create, and strips it on delete/re-parent (extend the existing reconcile).
- **Accurate LDP headers.** `Link: rel=type` must reflect reality —
  `ldp:DirectContainer` (+ `ldp:Container`, `ldp:RDFSource`, `ldp:Resource`) for
  collection endpoints, `ldp:RDFSource`/`ldp:Resource` for leaf records; advertise
  the type's SHACL shape via `Link: rel="http://www.w3.org/ns/ldp#constrainedBy"`;
  keep `Accept-Post`/`Accept-Patch`/`Allow`/`ETag`.
- **Seed + RD coherence.** Stop seeding `ldp:BasicContainer`; seed/derive
  `ldp:DirectContainer` with its membership config from the RD child links
  (root + each typed collection). Reconcile already-applied deployments (a
  membership-backfill, same shape as the schema-namespace migration).
- **Conformance suite** `tests/conformance/test_ldp.py` (testcontainers, real
  store): encode the LDP MUST/SHOULD as HTTP-level checks — methods + status
  codes; `Link` type set; content negotiation (Turtle/JSON-LD/RDF-XML/N-Triples);
  `POST` → 201 + `Location` + `Slug`, and the new member appears via
  `hasMemberRelation` on the container; `PUT` create/replace + `If-Match`
  (412/428); `PATCH` `application/sparql-update` post-state SHACL; `DELETE`;
  `OPTIONS` `Allow`; container membership/containment triples; 4xx paths. Writes
  run under a steward/admin context (or a test offer permitting them).
- **Deviations, documented not hidden.** The FDP keeps its custom `X-FDP-Page-*`
  paging (not LDP Paging / `Prefer`) and SPARQL-Update PATCH only (no LD-PATCH,
  ADR-0008). Capture an LDP conformance matrix (MUST/SHOULD/MAY × conformant /
  deviation / gap) under `docs/`, and **amend ADR-0008** to match the shipped
  reality.
- Optional external check: run the W3C `ldp-testsuite` against a dev instance and
  record the report.

References: ADR-0008, architecture §6 + conformance, W3C LDP
(https://www.w3.org/TR/ldp/), `metadata/ldp/router.py`, `metadata/containment.py`,
`metadata/profiles/applier.py`.

### 15.2 [x] DCAT v3 + FDP-O modular profile schemas (composed) + shape-closure validator

> **(a) shape-graph closure validator — DONE.** `ShaclValidator._load` now
> assembles the closure: from the requested shape it transitively resolves and
> merges every referenced shape (`sh:node`, `sh:and`/`sh:or`/`sh:xone` lists,
> `sh:qualifiedValueShape`, `sh:not`) via the `ShapeProvider`, so composed shapes
> validate. The merged closure is cached under the root IRI; `invalidate(iri)`
> **cascades** (editing a base shape drops every composed closure that imports
> it); an unresolvable referenced shape is tolerated (logged, skipped) while the
> root still validates. `_referenced_shape_iris` is the single place the
> composition predicates are followed. Backward-compatible — a non-composed
> shape's closure is just itself. Tests: 5 new cases in
> `tests/unit/metadata/test_shacl.py` (inherited-constraint enforcement,
> A→B→Resource transitivity via sh:node+sh:and, fetch-once-and-cache, cascade
> invalidation, unresolvable-ref tolerance).
>
> **(b) modular schema set + wiring — DONE.** Authored the modular default
> profile: `resource.ttl` (base mixin, no targetClass) + `dataset` (sh:node
> resource) + `catalog` (sh:node dataset) + `data-service` (sh:node resource) +
> `distribution` (standalone) + FDP-O `metadata` (sh:node dataset) +
> `metadata-service` (sh:node data-service, `fdp-o:servesMetadata`) +
> `fairdata-point` (sh:node metadata-service). Shapes identify themselves and
> reference each other via the `urn:fdp-schema:<slug>` placeholder; the applier
> (`iri.expand_schema_refs`) expands every placeholder to the deployment storage
> IRI on write so subjects + `sh:node` targets resolve. `profile.yaml` rewired to
> 8 schemas, root RD = `fdp-o:FAIRDataPoint` (served via `fdp-o:servesMetadata`).
> Fixed a latent bug: the root seed was typed with the schema's *storage* IRI
> (10.5) not the class IRI, so `sh:targetClass` never matched — now class IRI.
> `/spec` returns the merged closure (validator wired into the extensions
> router). Added `fdp-o`/`rdfs`/`skos`/`spdx` prefixes. Tests:
> `test_iri.test_expand_schema_refs…`, profile validates (8 schemas/5 RDs),
> integration `tests/integration/metadata/test_modular_schemas.py` (closure in
> `/spec`, composition enforced on write — a Catalog without inherited
> `dct:title` 422s — root seeded as fdp-o:FAIRDataPoint).
>
> **Existing-deployment migration — DONE (2026-06-12).**
> `metadata/profiles/migrate_modular.py` (`migrate_to_modular_profile`) + the
> `fdp profile migrate-modular <bundle>` CLI reconcile a pre-15.2 deployment to
> the modular profile **non-destructively**, the prod alternative to a
> `force-apply` re-bootstrap (which re-seeds — and so clobbers — the root):
> rewrite the schemas (placeholders expanded, skip-if-identical via RDF
> isomorphism + validator cache invalidation), rebuild the RD records (skip via
> *parsed-record* value equality — RD graphs carry `xsd:string` literals that
> some stores canonicalize on round-trip, so a graph compare false-positives),
> drop orphaned RD records (the root rename Repository→FAIRDataPoint changes the
> record slug; leaving the old one would put two empty-prefix roots in the
> startup cache rebuild), and **re-type the root record in place** —
> `fdp:Repository`→`fdp-o:FAIRDataPoint`, membership reset to
> `fdp-o:servesMetadata` — preserving authored title/rights and every member
> record. Idempotent (validates the bundle first; a second pass is a no-op).
> Member records keep their unchanged DCAT types and re-validate lazily on next
> write. Tests: unit `tests/unit/metadata/profiles/test_migrate_modular.py`
> (re-type + config write + idempotency + no-root safety) and integration
> `tests/integration/metadata/test_modular_migration.py` (apply a legacy
> `fdp:Repository` profile over Oxigraph → author a Catalog → migrate to the
> **shipped** `profiles/default` → root re-typed, orphan root RD removed, single
> correct rebuilt cache, authored Catalog survives, composed schema landed,
> second pass no-op).
>
> **Exhaustive DCAT 3 property coverage — DONE (2026-06-12).** The modular shapes
> now carry the full DCAT 3 property set: `resource.ttl` gained `dct:type`,
> `dct:relation`, `dct:provenance`, `dct:source`, `dcat:qualifiedRelation`, and the
> DCAT 3 §9 versioning chain (`dcat:version`, `adms:versionNotes`,
> `dcat:hasVersion`, `dcat:hasCurrentVersion`, `dcat:previousVersion`,
> `dct:isVersionOf`, `dct:replaces`); `dataset.ttl` gained `prov:wasGeneratedBy`;
> `distribution.ttl` gained `dct:issued`/`dct:modified`/`dct:rights`/
> `dct:accessRights`/`dct:conformsTo`/`dct:language`/`dcat:temporalResolution`/
> `dcat:spatialResolutionInMeters`. **Strictness is unchanged and deliberately
> lenient** — only `dct:title` is mandatory; every added property is optional with
> a `sh:datatype`/`sh:nodeKind` constraint, so the change is purely additive and
> existing records keep validating. (Making properties *mandatory* remains the
> deferred post-v0.2 community decision — that is the only open 15.2 item, and it
> is a policy call, not a build gap.) Verified: `validate_profile` passes on the
> 8-schema bundle; the SHACL closure tests + `test_modular_schemas` integration
> (a record validates against the composed closure over a real store) stay green.

**Decision:** rebuild the default profile schemas as a modular set composed along
the **faithful DCAT 3 subclass chain, extended with FDP-O**
(https://raw.githubusercontent.com/FAIRDataTeam/FDP-O/master/fdp-ontology.owl):

```
dcat:Resource
├── dcat:Dataset            (⊑ Resource)
│   ├── dcat:Catalog        (⊑ Dataset)
│   └── fdp-o:Metadata      (⊑ Dataset)
├── dcat:DataService        (⊑ Resource)
│   └── fdp-o:MetadataService (⊑ DataService)   --fdp-o:servesMetadata--> fdp-o:Metadata
│       └── fdp-o:FAIRDataPoint (⊑ MetadataService)
└── dcat:Distribution       (standalone)
```

**(a) Enabler — validator shape-graph closure.** Today `ShaclValidator._load`
fetches a *single* shape graph by IRI, so a shape that references another via
`sh:node <IRI>` won't resolve — composition is impossible. Change the loader to
assemble the **closure**: from the requested shape, transitively resolve every
referenced shape IRI (`sh:node`, `sh:property`→`sh:node`, `sh:and`/`sh:or`/
`sh:xone` lists, `sh:qualifiedValueShape`) through the `ShapeProvider` and merge
into one graph before pySHACL (`advanced=False` is fine — these are SHACL Core).
Cache the merged closure keyed by the root IRI; the 10.1/10.2 invalidate hooks
must **cascade** (editing a base shape invalidates every composed shape that
imports it). The `/spec` read endpoint (`GET /{type}/spec`) must return the
**merged** closure so the client's DASH form renderer sees inherited properties
(ties into the reference-widget work).

**Reference convention.** Composed shapes reference base shapes by their
*storage* IRI (`{base}/fdp-api/schemas/{slug}`), which the profile TTL can't
hardcode. The applier rewrites `sh:node`/list references from a manifest
placeholder (e.g. a `fdp-schema:` CURIE) to the storage IRI at apply time —
exactly the rewrite pattern 10.5 introduced for RD `constrainedBy`.

**(b) The modular schema set** (one schema record each, `/fdp-api/schemas/`):

- `ResourceShape` (`dcat:Resource`) — DCAT 3 common props: `dct:title` [1..1],
  `dct:description`, `dct:publisher`, `dct:creator`, `dct:contactPoint`,
  `dct:issued`, `dct:modified`, `dcat:keyword`, `dcat:theme`, `dct:language`,
  `dct:license`, `dct:accessRights`, `dct:conformsTo`, `dct:identifier`,
  `dcat:landingPage`, `dct:rights`, `prov:qualifiedAttribution`, …
- `DatasetShape` (`dcat:Dataset`) = `sh:node ResourceShape` + `dcat:distribution`,
  `dct:spatial`, `dct:temporal`, `dcat:temporalResolution`,
  `dcat:spatialResolutionInMeters`, `dct:accrualPeriodicity`, `dcat:inSeries`, …
- `CatalogShape` (`dcat:Catalog`) = `sh:node DatasetShape` + `dcat:dataset`,
  `dcat:service`, `dcat:catalog`, `dcat:record`, `dcat:themeTaxonomy`,
  `foaf:homepage`, `dct:hasPart`.
- `DataServiceShape` (`dcat:DataService`) = `sh:node ResourceShape` +
  `dcat:endpointURL`, `dcat:endpointDescription`, `dcat:servesDataset`,
  `dcat:accessService`.
- `DistributionShape` (`dcat:Distribution`, standalone) — `dcat:accessURL` [1..],
  `dcat:downloadURL`, `dcat:mediaType`, `dct:format`, `dcat:byteSize`,
  `dcat:compressFormat`, `dcat:packageFormat`, `dct:license`, `dcat:accessService`,
  `spdx:checksum`.
- `MetadataShape` (`fdp-o:Metadata`) = `sh:node DatasetShape` + FDP-O specifics.
- `MetadataServiceShape` (`fdp-o:MetadataService`) = `sh:node DataServiceShape` +
  `fdp-o:servesMetadata` (declare as `rdfs:subPropertyOf dcat:servesDataset`).
- `FAIRDataPointShape` (`fdp-o:FAIRDataPoint`) = `sh:node MetadataServiceShape` +
  FDP root props (`dct:title`, `dct:hasVersion`, `foaf:homepage`,
  `dct:conformsTo` the FDP spec, `dct:rights` for the system-default Offer).

Pull exact property lists from the DCAT 3 Rec (https://www.w3.org/TR/vocab-dcat-3/)
and the FDP-O OWL. **Strictness (v0.2):** keep it lenient — `dct:title` mandatory,
everything else optional with `sh:datatype`/`sh:nodeKind` constraints — so existing
records still validate; tightening is a post-v0.2 community decision.

**Profile + wiring changes.**

- Rewrite `profiles/default/schemas/*.ttl` into the modular set above
  (+ the FDP-O modules); declare them all in `profile.yaml` `schemas:` (leaves
  first, per the ordering note). Add `fdp-o`/`spdx` prefixes to the namespace
  registry.
- `resourceDefinitions`: root becomes `fdp-o:FAIRDataPoint`; child links use the
  typed relations (`fdp-o:servesMetadata` / `dcat:catalog` / `dcat:dataset` /
  `dcat:service` / `dcat:distribution`) reflecting the new hierarchy; RD
  `constrainedBy` points at the composed type shapes.
- Re-typing the root from `fdp:Repository` → `fdp-o:FAIRDataPoint` needs a
  reconcile for already-applied deployments (root seed + RD edits), same shape as
  the schema-namespace migration.
- Keep the meta-metadata schema separate (server-managed).

**Tests.** unit — closure assembly + cascade invalidation; a record validates
against base+delta and fails when an *inherited* required prop is missing; the
applier's `sh:node` reference rewrite. profile-validation — the new bundle passes
`validate_profile`. integration — author a FAIRDataPoint → Catalog → Dataset →
Distribution that satisfy their composed shapes over GraphDB/Oxigraph; `/spec`
returns the merged closure.

References: DCAT 3 (https://www.w3.org/TR/vocab-dcat-3/), FDP-O (linked OWL),
10.5 (schema namespace + reference rewrite), `metadata/shacl.py`,
`metadata/shape_provider.py`, `metadata/profiles/applier.py`,
`metadata/extensions.py` (`/spec`), `shared/namespaces.py`.

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

Personal TODO for v0.2
- Validate LDP interfaces: check whether the server correctly implemnt a LDP server
- Re-write the default profile schemas with complete DCAT v3. Modularize, i.e., create the Resources, Catalog, Dataset, DataService and Distribution from DCAT and the Repository and FAIRDataPoint from the FDP Ontology. Then make the resources, FDP, Catalog, Dataset, Data Service and Distribution by composing DCAT's Resource schema with the related schemas.

---

## Phase 16 — v0.3.0 release: persistent identifiers (FAIR F1)

Make record/schema/policy/data identifiers **globally unique and persistent** by
decoupling a record's identity from its serving host, honoring client-supplied
identifiers, and automating W3ID redirect setup. Full design + decisions in
[ADR-0014](docs/adr/0014-persistent-identifiers.md) and the approved plan. Two
new top-level concepts: `IDENTIFIER_BASE` (the persistent PID namespace records
are minted under) vs `BASE_URL` (the serving origin a redirector points to);
`IDENTIFIER_BASE` defaults to `BASE_URL` so localhost is unchanged.

### 16.1 [x] Config: `identifier_base` + `PIDSettings`

Top-level `identifier_base: HttpUrl | None` with `resolved_identifier_base` /
`serving_base` accessors; `pid: PIDSettings` subgroup (`FDP_PID_*`: w3id prefix,
GitHub token, fork owner, host allow-list). `.env.example` +
`docs/security/deployment-hardening.md` updated. (`src/fdp/config.py`)

### 16.2 [x] Canonicalization helper (`shared/identifiers.py`)

Pure `canonicalize(request_url, identifier_base, serving_origins)` + `is_under()`
+ `relative_path()`, mapping an inbound serving-origin request to its canonical
identifier-base IRI (sub-path deployments + unknown-host fallback handled).
Unit-tested (`tests/unit/shared/test_identifiers.py`, 17 cases).

### 16.3 [x] Mint all IRIs under `identifier_base`

`IRIExpander` and every managed-doc / registry / service construction site
(`main.py`, `cli.py`, schemas/policies/licenses/RDs/data) switched from
`base_url` to `resolved_identifier_base`. Registry cache resolves against it.
Defaults to `base_url`, so localhost is unchanged (LDP + profile/registry unit
suites green).

### 16.4 [x] LDP router canonicalization

`build_ldp_router` takes `identifier_base` + `serving_origins`; `_resource_iri`
→ `_request_url` + a `_canonical_iri` closure used by every handler; wired in
`main.py`. Cross-host stability covered by `tests/unit/metadata/ldp/test_router_pid.py`
(PUT stores + reads under the canonical IRI regardless of serving host).

### 16.5 [x] Dual identifier model on write (`metadata/identifiers.py`)

`reconcile_identifiers()` — rebind a foreign primary subject to the canonical IRI
+ add `owl:sameAs`; preserve client `dct:identifier`/`owl:sameAs`/
`skos:exactMatch`; within-base honoring via PUT-path / POST-`Slug`. Wired into
PUT + POST. `resource.ttl` SHACL shape gained optional `dct:identifier` (already
present) + `owl:sameAs` + `skos:exactMatch`. Tested:
`tests/unit/metadata/test_identifiers.py` + router-level cases.

### 16.6 [x] `/config` exposes identifier + serving bases

`identity/bootstrap.py`: `fdp_url` = canonical identifier base; new `serving_url`
= serving origin. Coincide in dev. Tested in `tests/unit/identity/test_bootstrap.py`.

### 16.7 [x] PID tooling + `fdp pid` CLI (`metadata/pid/`)

`w3id-config` (emit `.htaccess` + README), `w3id-pr` (opt-in GitHub fork+PR,
reusable to update the redirect target when the deployment moves), `verify`
(resolution test: the PID redirects and resolves to this FDP), `rebase` (one-time
adoption migration for a pre-PID deployment). 17 unit tests in
`tests/unit/metadata/pid/` (pure w3id, respx-mocked GitHub + verify, fake-adapter
rebase).

### 16.8 [x] ADR-0014 + architecture docs

[ADR-0014](docs/adr/0014-persistent-identifiers.md) written + indexed; the
architecture README open-questions list updated with the resolution.

### 16.9 [~] Release: quality gate, version bump, client coordination

Quality gate green (`ruff` clean, `pyright` 0 errors, 999 unit tests pass — also
fixed 7 pre-existing pyright errors in the migrate-modular tests). Integration:
`tests/integration/metadata/test_persistent_identifiers.py` (testcontainers
Oxigraph + Postgres) — 3/3 pass: `/config` exposes distinct bases; a record
created on the serving host is minted/stored under the canonical IDENTIFIER_BASE
IRI and resolves via the serving path; a foreign subject is rebound + kept as
`owl:sameAs`. Bumped `pyproject.toml` + `uv.lock` to `0.3.0`; `__version__` now
derives from package metadata (was stale at 0.1.0). **Remaining:** tag after
merge; coordinate `fdp-client` (see below).

**Out of scope (v0.3.0):** backup/restore (enabled by this work, built later);
honoring *foreign* IRIs as the dereferenceable subject; DOI/Handle minting.

---

## Phase 17 — Alternative identifiers + FAIR Signposting (ADR-0017) and write-path hardening (ADR-0016 §1)

Implements [ADR-0017](docs/adr/0017-alternative-identifiers-and-signposting.md)
(amends ADR-0014's dual identifier model; adds FAIR Signposting Level 1) and the
two LDP contract fixes decided in
[ADR-0016 §1](docs/adr/0016-backup-restore-migration.md). Read both ADRs before
starting — they record the decisions and the rejected alternatives; do not
improvise around them.

> **Status: complete — released as v0.4.0 (2026-07-03).** All of Phase 17 shipped:
> write-path hardening (`400` ambiguous body, `409` `Slug` collision, 17.1), the
> `owl:sameAs` → `adms:identifier` semantics change (17.2), the FAIR Signposting
> Level-1 link builder (17.3) wired into `GET`/`HEAD` (17.4), and docs + release
> (17.5) — plus two fixes uncovered while testing (managed-resource `/state`
> resolution; the repository-root `/page` trailing-slash bug) on a repaired,
> GraphDB-backed integration suite. See `CHANGELOG.md`. The remaining ADR-0016
> work (`fdp dump`/`restore`/`import`) is deferred to a later phase.

### 17.1 [x] Write-path hardening: ambiguous body → 400, POST Slug collision → 409

- `metadata/identifiers.py` (`reconcile_identifiers`): remove the
  "store as authored" fallback. A write body must address the record as `<>` /
  the canonical IRI, or contain exactly one typed IRI primary subject. Zero
  typed subjects, multiple typed subjects, or blank-node-only bodies raise a
  new `AmbiguousSubject` error (→ HTTP 400 via the shared error envelope) whose
  message states the requirement. Signature change: the function may now raise;
  update the router call sites (PUT + POST).
- `metadata/ldp/router.py` (`http_post`): before `put_graph`, check whether the
  minted member IRI already exists (`repo.get_graph` non-empty **or** meta graph
  has `dct:created` — match the 404 convention). If it exists → `409 Conflict`
  (add a `Conflict` error to `shared/errors.py` if the mapping is missing),
  telling the client to pick another Slug or use `PUT` + `If-Match`. POST must
  never overwrite.
- Update existing tests that relied on the lenient behaviours
  (`tests/unit/metadata/test_identifiers.py::test_ambiguous_multiple_typed_subjects_left_as_is`
  now expects a raise); add router-level cases for both new status codes.

References: ADR-0016 §1, ADR-0014 §3, `docs/dev-docs/04-request-lifecycle.md`.

### 17.2 [x] `reconcile_identifiers`: foreign subject → adms:identifier, never server-minted sameAs

- `shared/namespaces.py`: add `ADMS = Namespace("http://www.w3.org/ns/adms#")`.
- `metadata/identifiers.py`: when rebinding a foreign primary subject, replace
  the automatic `owl:sameAs` with:
  `<canonical> dct:identifier "<foreign-iri>"` (plain literal) **and**
  `<canonical> adms:identifier [ a adms:Identifier ; skos:notation
  "<foreign-iri>"^^xsd:anyURI ]`. Within-base mis-addressing keeps getting
  silently corrected with no cross-reference (unchanged). Client-authored
  `owl:sameAs` / `skos:exactMatch` / `dct:identifier` / `adms:identifier` on
  `<>` still pass through untouched. Update the module docstring — it documents
  the old sameAs behaviour.
- `profiles/default/schemas/resource.ttl`: add optional `adms:identifier`
  (additive/lenient, same posture as the v0.3.0 `owl:sameAs` addition).
- No data migration: existing server-minted `sameAs` triples are
  indistinguishable from client-authored ones (ADR-0017 §1); note the
  semantics change in release notes.
- Tests: rewrite the foreign-subject cases in
  `tests/unit/metadata/test_identifiers.py`; update the integration assertion
  in `tests/integration/metadata/test_persistent_identifiers.py` (currently
  expects `owl:sameAs`).

References: ADR-0017 §1; DCAT 3 `adms:identifier`.

### 17.3 [x] `metadata/signposting.py` — pure Link-relation builder

New module, pure functions (no I/O), mirroring the discipline of
`shared/identifiers.py`:

- `PID_RESOLVERS`: frozen set/predicate for recognised PID resolvers —
  `doi.org`, `dx.doi.org`, `hdl.handle.net`, `w3id.org`, `purl.org`,
  `identifiers.org`, plus `ark:` scheme IRIs.
- `select_cite_as(record_graph, canonical_iri) -> str`: ADR-0017 §2 order —
  client-asserted `owl:sameAs` under a PID resolver → IRI-valued
  `adms:identifier`(`skos:notation`)/`dct:identifier` under a PID resolver →
  the canonical IRI. Deterministic tie-break (lexicographic) when several
  qualify.
- `signposting_links(record_graph, canonical_iri, media_types) -> list[Link]`
  producing typed relations: `cite-as`; `describedby` (canonical IRI once per
  supported RDF media type, with `type` attribute — reuse
  `shared/negotiation.SUPPORTED_TYPES`); `type` (each `rdf:type` of the
  canonical subject); `license` (`dct:license`); `author` (IRI-valued
  `dct:creator`/`dct:publisher`); `item` (objects of the container's typed
  member relations + `ldp:contains`); `collection` (`dct:isPartOf`).
- Cap total signposting links per response (e.g. 30, constant) so a huge
  container cannot blow up the header block; when the cap trims `item` links,
  that is acceptable Level-1 degradation (Level-2 linkset is deferred —
  ADR-0017 §2).
- Serialization helper to RFC 8288 header syntax; unit tests
  (`tests/unit/metadata/test_signposting.py`) cover selection order, PID
  recognition (incl. `ark:`), attribute quoting, and the cap.

References: ADR-0017 §2; FAIR Signposting Profile (signposting.org/FAIR/);
RFC 8288.

### 17.4 [x] Wire signposting into LDP GET/HEAD

- `metadata/ldp/router.py`: in `http_get` and `http_head` (existing records
  only — after the 404 check), extend the `Link` header via the 17.3 builder.
  Keep the LDP `rel="type"` + `constrainedBy` links first; signposting links
  append to the same comma-joined header. The graph is already in hand — no
  extra store round-trip.
- `cite-as` must use the **canonical** IRI as default even when the request
  arrived on a serving origin (pass the canonicalized IRI, which the handlers
  already have).
- Router-level tests: record with a client-asserted DOI `sameAs` → `cite-as`
  is the DOI; plain record → `cite-as` is the canonical IRI; container GET
  carries `item` links; HEAD carries the same links as GET.
- Contract test: `tests/contract/` asserts a published record's response
  headers parse as valid RFC 8288 and include exactly one `cite-as`.

References: ADR-0017 §2; LDP §4.2.1 (existing Link discipline in
`_response_headers`).

### 17.5 [x] Docs + release for v0.4.0 identifier work

- `docs/dev-docs/04-request-lifecycle.md`: document the new 400/409 paths and
  the signposting header stage.
- `docs/conformance/`: add a FAIR Signposting Level 1 conformance note (which
  relations are emitted, the cap, the Level-2 deferral).
- Release notes: the two LDP contract changes (400 ambiguous body, 409 Slug
  collision), the sameAs → adms:identifier semantics change, new headers.
- Quality gate: `ruff` clean, `pyright` 0 errors, full unit + integration
  suites green. Coordinate `fdp-client`: render `adms:identifier` in record
  detail; surface `cite-as` as the "Cite this" URI; add
  `<link rel="cite-as">` to the record page head.

References: ADR-0016, ADR-0017, `docs/dev-docs/07-contributing.md`.

**Out of scope (Phase 17):** Level-2 linkset endpoint; DOI/Handle minting.
(`fdp dump`/`restore`/`import` are Phase 18, below.)

---

## Phase 18 — Backup / restore / migration (ADR-0016 §2–§6)

**Status: complete** (branch `feature/adr-0016-backup`, for v0.7.0). Commits: 18.2 `80a3c41`, 18.3 `8fced31`, 18.4 `3e330c6`, 18.6 `5e50035`, 18.5 `13ee48c`; runbook `docs/dev-docs/08-backup-restore.md`; ADR-0016 → Accepted.

Implements the storage-level backup/restore/import from
[ADR-0016](docs/adr/0016-backup-restore-migration.md); §1 (write-path hardening)
already shipped in v0.4.0 (task 17.1). Read the ADR first.

**Binding-aware (ADR-0019).** Profiles, schemas, and their immutable version
snapshots are ordinary named-graph records, and each record carries its own
`dct:conformsTo` (+ `fdp-o:validatedAgainst` in `/meta`) — so a **quad dump
captures the binding for free and is self-validating**. Two consequences: the
manifest records a **data-model version** so `restore` can migrate across the
ADR-0019 transition (keeping dump/restore independent of Phase 20); and
**import-with-validation** and the **rebase rewrite** must understand the ADR-0019
cross-references (`conformsTo`, `validatedAgainst`, `prof:hasArtifact`, profile /
schema / version IRIs). Target version: v0.5.0+ (sequence vs Phases 19/20 TBD).

### 18.1 [x] Close the write-path holes (`400` ambiguous body, `409` Slug collision)

Shipped in v0.4.0 (task 17.1); listed here for ADR-0016 completeness.

### 18.2 [x] `fdp dump` — storage-level export

_Done (commit 80a3c41): `fdp backup dump` in `metadata/backup/dump.py`._

- CLI (admin-operated, adapter-level like `fdp pid rebase`): `records.nq` = **every
  named graph** in the store (record + `/meta` + `/audit` siblings, plus the
  reserved profile / schema / resource-definition / policy / license graphs and
  their immutable version snapshots — all captured because they are named graphs),
  serialized as N-Quads. Nothing is interpreted on the way out.
- `manifest.json`: dump-format version, `identifier_base`, application version,
  **data-model version** (whether the ADR-0019 binding is present), graph count,
  per-file checksums, timestamp.
- `audit.jsonl` (optional): the Postgres `record_audit` rows.
- Reads through the `TripleStoreAdapter` directly; the LDP layer is not involved.

References: ADR-0016 §2; ADR-0019 §5 (profiles/versions are named graphs).

### 18.3 [x] `fdp restore` — faithful, verbatim import

_Done: `fdp backup restore` in `metadata/backup/restore.py` (verbatim load, base/empty preconditions, --merge/--overwrite/--dry-run, audit insert, legacy→ADR-0019 migration, search reindex via shared `search/reindex.py`)._

- Load the quads verbatim through the adapter — **no `MetaWriter` stamping**, so
  `dct:created`/`dct:modified`/creator/state, `dct:conformsTo`/`validatedAgainst`,
  and the audit graphs survive exactly.
- **Precondition:** target `identifier_base` == the manifest's; on mismatch refuse
  and point at `fdp import --rebase`.
- Refuse a non-empty store unless `--merge` (skip existing graphs) or `--overwrite`.
- **Cross-version restore:** if the manifest's data-model version predates ADR-0019,
  run the Phase-20 migration after load (backfill `conformsTo`/`validatedAgainst`,
  wrap schemas as profiles) so the restored instance is self-describing.
- Afterwards: reindex `metadata_search`; insert `audit.jsonl` rows when present.
  `--dry-run` reports what would change.

References: ADR-0016 §3; ADR-0019 §6 (migration).

### 18.4 [x] `fdp import --rebase` — adoption from an FDPneo dump under a different base

_Done: `fdp backup import --rebase` (restore_store rebase mode) re-roots graph IRIs + all IRI terms old→new via the shared `pid/rebase` rewrite, covering the ADR-0019 cross-references._

- Compose restore with `pid/rebase.py`'s term rewriting applied in-flight: re-root
  every IRI under the old base to `identifier_base`, cross-record links included.
- **Binding-aware rewrite:** the rewrite set must also cover the ADR-0019
  cross-references — `dct:conformsTo` (→ profile stable IRI), `fdp-o:validatedAgainst`
  (→ profile version IRI), `prof:hasArtifact` (→ shape version IRI), and the profile
  / schema graph IRIs and their version children.
- One-time, like `rebase`; reindex search after; `record_audit` intentionally keeps
  the historical IRIs (document the Postgres boundary).

References: ADR-0016 §4 (FDPneo dump), §6; `pid/rebase.py`.

### 18.5 [x] `fdp import` — migration from a reference-FDP instance (depends on Phase 20)

_Done: `fdp backup import --from <url>` (metadata/backup/import_fdp.py) crawls the source LDP tree (BFS over ldp:contains, egress-pinned to the source origin), re-roots each IRI to identifier_base via the shared rebase rewrite, carries source dct:issued/modified → meta created/modified (privileged write path 18.6), and preserves the old IRI as adms:identifier (ADR-0017). The CLI then binds imported records to this deployment's profiles via backfill_conformance + reindexes. Validation-as-report hook present; foreign sources (no conformsTo) are bound by the backfill._

- Walk the source LDP tree (or consume its export); map each host-bound IRI to
  `identifier_base` + the same path; carry provenance (source
  `dct:issued`/`dct:modified` → meta `dct:created`/`dct:modified`).
- **Profiles first, then validate:** import the source's profiles/schemas, then for
  each record resolve **its own** `dct:conformsTo` → profile → shape and validate —
  as a **report**, not a hard reject (ADR-0016 §3 posture). Requires the ADR-0019
  binding (**Phase 20**).
- Preserve the old IRI as a structured alternative identifier (`adms:identifier` +
  `dct:identifier`, ADR-0017); `owl:sameAs` only on explicit operator assertion.
  When the old host will not keep resolving, record the mapping once in the import
  report instead of on every record.

References: ADR-0016 §4 (reference FDP); ADR-0017 §1; ADR-0019 §1. **Depends on Phase 20.**

### 18.6 [x] Privileged provenance write path (internal, CLI-only)

_Done: `MetadataRepository.write_imported` + `build_meta_graph(created=…, modified=…)` write meta graphs with supplied provenance (dct:created/modified/creator/state), bypassing now-stamping. CLI-only; never on the HTTP surface (ADR-0016 §5). Consumed by 18.5._

- Restore/import must write meta graphs with *supplied* timestamps/creator/state
  and write audit graphs. Add an internal repository path used **only** by these
  CLI commands — never an LDP header or query flag. The HTTP contract stays
  ADR-0014's: canonical subject always, server-stamped provenance always.

References: ADR-0016 §5.

### 18.7 [x] Docs + release

- Operator runbook: dump / restore / import, the mandatory search-reindex step,
  and the `record_audit` IRI-history boundary (ADR-0016 §6). CHANGELOG; version
  bump; quality gate green (`ruff`, `pyright`, full unit + integration).

References: ADR-0016 §6.

**Out of scope (Phase 18):** DOI/Handle minting; a hosted/scheduled backup service;
partial or selective dumps (whole-store only for v1).

---

## Phase 19 — Agent consumption: server-side support for the `fdp-mcp` sidecar (ADR-0018)

The MCP bridge itself is a separate repo (`../mcp`) with its own `TASKS.md` —
per [ADR-0018](docs/adr/0018-agent-consumption-mcp-server.md), it consumes
only the public FDP surface and works against any spec-compliant FDP. This
phase covers what the **server** repo owes the programme. Motivation and
phasing: [`docs/architecture/agent-consumption-vision.md`](docs/architecture/agent-consumption-vision.md).
`(Phase 18 is reserved for ADR-0016 backup/restore.)`

**Status: complete** (branch `feature/adr-0018-agent-support`). The bridge
(`../mcp`, v0.1.0) was already done; this phase closed the server-side obligations.
Gap triage lives in [`docs/conformance/agent-ready.md`](docs/conformance/agent-ready.md)
and the per-gap table below. The one actionable server capability gap — **G-05**
(endpoint discovery) — is fixed (19.2); G-01/G-02/G-03/G-04 are interop/spec
concerns FDPneo already satisfies; G-06 (shape closure) is deferred (FDPneo already
serves the closure via `?composed=true` / `/{prefix}/spec`). Commits: 19.2/G-05
`b13972d`, 19.1 `8b428f7`.

### 19.1 [x] Wire `fdp-mcp` into the standard deploy profiles

- Add the `fdp-mcp` service to the compose/deploy profiles (`deploy/`,
  architecture §12), pointed at the FDP container's base URL, so a default
  FDPneo deployment comes up agent-ready (ADR-0018 §6). Off switch documented.
- Smoke test in the deploy checks: bridge answers an MCP `initialize` and a
  `fdp_info` call against the running FDP.
- Docs: one paragraph + link to `../mcp/docs/agent-quickstart.md` from the
  server README and deployment docs.

References: ADR-0018 §6, architecture §12.

### 19.2 [x] Gap-report triage loop

- Adopt `../mcp/docs/fdp-api-gaps.md` as a standing input to this file:
  each triaged gap becomes a task here (or an explicit won't-fix with
  rationale), linked both ways.
- First triage pass once mcp Phase 1 lands: expected early candidates —
  search API discoverability for external clients, shape retrieval for
  runtime-defined kinds, JSON-LD framing quality of record GETs, and any
  place FDPneo and the Java reference implementation diverge on the same
  request.

References: ADR-0018 §4.

### 19.3 [x] "Agent-ready FDP" conformance note

- `docs/conformance/`: a note recording which parts of the
  `../mcp/docs/mcp-tool-surface.md` contract this server's public surface
  supports (required vs optional tools), following the existing
  conformance-note practice. Update as gaps close.

References: ADR-0018 §5, vision doc §7.

**Out of scope (Phase 19):** the bridge implementation (see `../mcp/TASKS.md`);
Index-level MCP (vision increment C — needs Phase 8 and its own ADR);
capability-profile schema package (increment B).

---

## Phase 20 — Self-describing record–schema binding & versioning (ADR-0019)

Implements [ADR-0019](docs/adr/0019-record-schema-binding-and-versioning.md): make
records self-describing at rest by carrying `dct:conformsTo` → a `prof:Profile`
(the server-stamped, authoritative validation binding), demote the
ResourceDefinition to a type→profile index, and give schemas/profiles immutable
versioned identity. Read the ADR first — it records the decisions (stable binding
in the record + exact version in `/meta`; PROF minimal-but-growable; RD matches
its type's profile in v1) and the rejected alternatives; don't improvise.

This **amends ADR-0007/0009** and is the **prerequisite for Phase 18's `fdp import`
step** (import brings profiles first, then validates each record against its own
`conformsTo`). Target version: v0.5.0 (tentative; sequence vs Phases 18/19 TBD).

**Status: complete** (branch `feature/adr-0019-binding`, for v0.5.0). Implementation
notes: 20.3/20.4 landed with a decision refinement — the RD's `ldp:constrainedBy`
**stays on the schema**; the type's profile is **derived 1:1** from it (same slug,
auto-provisioned on schema publish), reaching the same self-describing outcome
without the churn of flipping `constrainedBy` everywhere. ADR-0019 §2 was amended
to record this. Commits: 20.1 `d1b2919`, 20.2 `e112506`, 20.3 `f719ee8`,
20.4 `95fcbac`, 20.5 `06846c9`, 20.6 `465e764`.

### 20.1 [x] PROF vocabulary + versioned profile/schema graph URIs (shared kernel)

- `shared/namespaces.py`: add `PROF` (`http://www.w3.org/ns/dx/prof/`) and the
  profile-role namespace (`role:validation`, …); register the `prof`/`role`
  prefixes.
- `shared/graphs.py`: `profile_graph_uri(base, slug)` →
  `{base}/fdp-api/profiles/{slug}` (stable) and a versioned variant
  `{base}/fdp-api/profiles/{slug}/{version}`; matching helpers for schema version
  IRIs; `is_profile_graph_uri`; add `profiles` to the managed-segment set (so
  `state_record_iri` / the quad dump treat it like schemas).
- Fix the version-IRI scheme once here — it is load-bearing for §20.2–20.4.

References: ADR-0019 §1/§4/§5; the existing `_*_SEGMENT` conventions.

### 20.2 [x] Immutable, versioned schema identity (schema service)

- `metadata/schemas.py`: change `PUT /schemas/{id}` from mutate-in-place to
  **snapshot** — write an immutable version graph, move `dcat:hasCurrentVersion`
  on the stable IRI, retain prior versions, bump `dcat:version`. `GET` returns the
  current version; add fetch-by-version. `schema_exists` resolves the current
  shape.
- Tests: version round-trip, current-pointer move, prior-version retention.

References: ADR-0019 §4.

### 20.3 [x] Profile resource type (PROF) — service, router, RD wiring

- New managed **Profile** resource: `prof:Profile` with
  `prof:hasResource [ prof:hasRole role:validation ; prof:hasArtifact <shape
  version> ]`. Service + router under `/fdp-api/profiles` mirroring
  `schemas.py`/`licenses.py`, versioned like §20.2.
- **Auto-provision:** publishing a schema creates/updates a profile wrapping its
  current shape version (v1: one profile per schema, maintained by the schema
  service).
- `metadata/profiles/rd_records.py` + RD admin/applier + container registry: the
  RD's default binding (`ldp:constrainedBy`) now references a **profile**, not a
  bare schema.

References: ADR-0019 §1/§2/§5.

### 20.4 [x] Write path: stamp `dct:conformsTo`, validate via profile, record `validatedAgainst`

- `metadata/ldp/router.py` (+ a containment-style maintainer): resolve
  type→RD→profile; inject/refresh `dct:conformsTo <stable profile>` in the record
  graph; **enforce record profile == type default** (reject a conflicting
  client-asserted validation `conformsTo`).
- Shape resolution for validation goes through the record's `conformsTo` → profile
  → `role:validation` artifact (adjust `ContainerRegistry.shape_for` /
  `member_shape` or the validator).
- `metadata/meta.py`: write `fdp-o:validatedAgainst <profile version>` into the
  meta graph at write time.
- Tests: `conformsTo` stamped/maintained; validation resolves via `conformsTo`;
  `validatedAgainst` recorded; conflicting client `conformsTo` rejected.

References: ADR-0019 §1/§2/§3.

### 20.5 [x] Resource shape, meta shape, docs

- `profiles/default/schemas/resource.ttl`: annotate `dct:conformsTo` as
  server-managed; `schemas/meta-metadata.ttl`: add optional
  `fdp-o:validatedAgainst`.
- `docs/dev-docs/04-request-lifecycle.md`: document the profile-driven validation
  stage; `docs/conformance/`: a profile-binding note. (A Signposting profile link
  relation is deferred — ADR-0017/0019.)

References: ADR-0019 §1/§3.

### 20.6 [x] Migration: backfill `conformsTo` + wrap schemas as profiles

- CLI command (adapter-level, idempotent, `--dry-run`, like `fdp pid rebase`):
  wrap each existing schema in a profile and snapshot it as version 1; for each
  record, resolve type→RD→profile, stamp `dct:conformsTo`, and write
  `validatedAgainst` (the v1 IRI) into its meta graph. Reindex search if
  `conformsTo` participates.

References: ADR-0019 §6.

### 20.7 [x] Quality gate + release (v0.5.0)

- `ruff` clean, `pyright` 0 errors, full unit + integration green. ADR-0019 status
  → Accepted; CHANGELOG; version bump; coordinate `fdp-client` (render
  `conformsTo`/profile; a validation view resolves the profile's shape).

References: ADR-0019.

**Out of scope (Phase 20):** per-record profile choice decoupled from the RD
(deferred, ADR-0019 §2); rich multi-resource profiles beyond `role:validation`
(the model is growable but v1 wraps a single shape); a Signposting profile link
relation.

## Phase 21 — External (remote) label resolution (ADR-0012 extension)

Third label source for `GET /fdp-api/labels`: dereference external identifiers
(ROR, DOI, ORCID, SKOS terms) over content-negotiated RDF, extract a label, and
serve it — **off by default**, allow-listed, SSRF-guarded, **lazy by default**
with an opt-in bounded `?wait`. Pre-sanctioned by ADR-0012 §8 / architecture §8.6
("remote-vocabulary labels", same posture as remote schema sync). Reuses the
shared `httpx.AsyncClient`, `shared.ssrf.assert_public_url`, the schema-sync
fetch/parse, and the search ORM/upsert patterns. Target release **v0.10.0**.

### 21.1 [x] Config: `RemoteLabelSettings`

- `src/fdp/config.py`: `RemoteLabelSettings` mirroring `SchemaSyncSettings`,
  `env_prefix="FDP_REMOTE_LABELS_"`: `enabled: bool = False`;
  `allowed_hosts: Annotated[list[str], NoDecode]` with the shared CSV/JSON
  `_split_hosts` validator (**empty denies all**); `timeout_seconds=5.0`,
  `max_bytes=256*1024`, `max_redirects=5`, `max_concurrent_fetches=4`,
  `positive_ttl_seconds=2592000`, `negative_ttl_seconds=86400`,
  `max_wait_ms=3000`; `@property effective_enabled` = `enabled and bool(hosts)`.
- Wire into `Settings` as `remote_labels` next to `index` / `schema_sync`.
- Tests: env parsing (CSV + JSON), `effective_enabled` gate.

References: ADR-0012 §8; architecture §8.6.

### 21.2 [x] Postgres persistent cache (table + repo)

- Migration `migrations/versions/0009_external_labels.py` (`down_revision="0008"`):
  table `metadata_external_labels` — composite PK `(iri String(2048),
  language String(32))`, `label Text NULL` (NULL = cached miss), `resolved_at`
  + `expires_at AwareDateTime` (index `expires_at`), `source_host String(255) NULL`.
- `src/fdp/metadata/external_labels.py`: `ExternalLabelRow(Base)` ORM (mirror
  `search/model.py`, `.with_variant(..., "sqlite")`); `ExternalLabelCache(session_factory)`
  with `get_many(iris, language)` (only `expires_at > now`) and
  `upsert(iri, language, label, *, ttl)` via `pg_insert(...).on_conflict_do_update`.
- Tests (SQLite variant): upsert→get_many, expiry filtering, negative rows.

References: CLAUDE.md (Postgres holds operational state; parameterized SQL).

### 21.3 [x] External fetcher (allow-list + SSRF + RDF extraction)

- `src/fdp/metadata/external_labels.py`: `ExternalLabelFetcher(http_client, settings)`
  `async fetch(iri) -> str | None`: `is_safe_iri` → initial host on allow-list →
  manual redirect loop (≤ `max_redirects`) calling `assert_public_url(url,
  allowed_hosts=...)` **on every hop** → streamed size-capped GET
  (`Accept: text/turtle, application/rdf+xml;q=0.9, application/ld+json;q=0.8`) →
  multi-format RDF parse (reuse `schema_sync._parse`).
- `best_label_from_graph(...)`: graph analogue of `_pick_best_labels`
  (`(language_band, predicate_rank)`) over `rdfs:label, skos:prefLabel, dct:title,
  foaf:name, schema:name` (add `foaf`/`schema` to `shared/namespaces.py` if missing).
- Concurrency: `asyncio.Semaphore(max_concurrent_fetches)` + in-flight dedupe set.
  **Preserve R-01**: no remote JSON-LD `@context` fetch.
- Tests: `httpx.MockTransport` — Turtle + JSON-LD resolve; scoring; off-allow-list
  skipped; private/loopback blocked; over `max_bytes` rejected; miss cached negative.

References: ADR-0012 §8; SECURE-DEVELOPMENT.md Rule 3; audit-2026-06-07 F-01/R-01/N-02.

### 21.4 [x] Resolver integration (lazy default + bounded wait)

- Extend `LabelResolver.__init__` with optional `external_cache`,
  `external_fetcher`, and a background task set.
- `lookup(iris, *, language, wait_ms=0)`: after graph + inline, for unresolved IRIs
  → (1) Postgres `get_many` (pos/neg short-circuit); (2) remaining allow-listed
  unknowns: `wait_ms>0` → bounded `asyncio.wait_for` gather, persist + include what
  returns; else (lazy) → schedule per-IRI background fetch (upserts Postgres + seeds
  in-memory), **omit** from this response.
- `effective_enabled` false → skip (1)+(2); behavior identical to today.
- Tests: in-memory > Postgres > fetch precedence; lazy omits + schedules; `wait`
  returns inline; disabled = current behavior.

References: labels.py §6.1 lookup strategy.

### 21.5 [x] Router param + app wiring + shutdown

- `labels.py` router: add `wait: Query(ge=0, le=max_wait_ms) = 0` (ms), pass to
  `lookup(..., wait_ms=wait)`.
- `src/fdp/main.py` `_build_shared_state`: build `ExternalLabelCache` +
  `ExternalLabelFetcher` (shared `http_client`, `session_factory`,
  `settings.remote_labels`), inject into `LabelResolver`; cancel/await outstanding
  background fetches in the lifespan `finally` (mirror IndexPinger / job-registry).

References: main.py lifespan; index_ping shutdown pattern.

### 21.6 [ ] Integration tests

- `tests/integration/metadata/`: real Postgres (testcontainers) — `ExternalLabelCache`
  upsert/get + expiry; resolver end-to-end with a mocked HTTP transport writing
  through to Postgres.

### 21.7 [ ] Docs + ADR

- Update `labels.py` module docstring (drop "deferred"; document the third source,
  allow-list, lazy/`wait` semantics). Add `FDP_REMOTE_LABELS_*` to the deploy `.env`
  example + `docs/dev-docs/`. Record the decision (amend ADR-0012 or short new ADR):
  trust/allow-list/caching/lazy semantics; caveat that redirect-based conneg needs
  terminal RDF hosts allow-listed (DOI → `data.crossref.org`/`data.datacite.org`).

### 21.8 [ ] Quality gate + release (v0.10.0)

- `uv run ruff check . && uv run ruff format --check . && uv run pyright &&
  uv run pytest` green. Manual E2E: `FDP_REMOTE_LABELS_ENABLED=true` +
  `FDP_REMOTE_LABELS_ALLOWED_HOSTS=ror.org,doi.org,orcid.org,api.ror.org,data.crossref.org`,
  rebuild/restart, `GET /fdp-api/labels?iri=https://ror.org/006hf6230` (first omits →
  background fetch → repeat returns; row in `metadata_external_labels`) and
  `...&wait=3000` inline. CHANGELOG + version bump; coordinate `fdp-client`.

**Out of scope (Phase 21):** per-source JSON adapters (ROR/Crossref JSON APIs) —
generic RDF conneg only; scheduled bulk refresh / expired-row purge CLI (minimal
purge may ride on 21.2); label resolution for access-controlled *local* records
(unchanged — external only).
