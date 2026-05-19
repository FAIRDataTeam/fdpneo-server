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

---

## Phase 0 — Foundations

These produce the skeleton everything else builds on.

### 0.1 Shared kernel: namespaces, errors, request context
- Implement `shared/namespaces.py` with the standard RDF prefix registry (DCAT, DCT, FOAF, LDP, ODRL, PROV, SH, XSD, plus an `fdp:` namespace).
- Implement `shared/errors.py` with `FDPError` base class, a small set of concrete errors (NotFound, Forbidden, Conflict, SchemaViolation, PolicyViolation), and the FastAPI exception handler that maps them to a structured JSON envelope.
- Implement `shared/context.py` with the immutable `RequestContext` dataclass (subject URI or anonymous sentinel, role set, request timestamp, trace ID).
- Implement `shared/logging.py` configuring structlog with the request-context binding.
- Implement `shared/events.py` — a minimal async in-process event bus (publish/subscribe with weak references).
- Unit tests for each module.

References: CLAUDE.md (Code conventions, RDF and namespaces), architecture §5.7, §14.1.

### 0.2 Postgres adapter with Alembic
- SQLAlchemy 2.x async engine and session factory in `storage/postgres/engine.py`.
- Alembic environment in `migrations/` configured to read the URL from settings.
- Initial migration creating empty tables for: `metrics_hourly`, `metrics_daily`, `authz_index`, `policy_decisions_audit`, `job_state`, `profile_applied`. Schema details TBD per consuming module; this migration just establishes the namespace.
- Integration test using testcontainers-postgres that runs the migration cleanly.

References: ADR-0003, architecture §4.4, §5.8.

### 0.3 Triple store adapter (SPARQL 1.1 Protocol)
- `storage/triplestore/adapter.py` exposing `query`, `update`, `ingest_graph`, `replace_graph`, `drop_graph`, `ask`.
- Use `httpx.AsyncClient`. Authentication via basic or bearer per configuration.
- Capability flags read from `TripleStoreSettings`; default implementations raise `NotImplementedError` for capabilities a backend lacks.
- Integration tests against testcontainers-launched GraphDB, Fuseki, and Oxigraph. The same test suite runs against all three; backends that lack a capability are skipped via marker.

References: ADR-0005, ADR-0007, architecture §4.3, §5.8.

---

## Phase 1 — Identity and access foundations

### 1.1 OIDC authentication middleware
- `identity/jwks.py` — JWKS fetch and cache against the configured issuer's OIDC discovery document.
- `identity/middleware.py` — FastAPI middleware that extracts the bearer token, validates it, builds the `RequestContext`, and attaches it to the request.
- `identity/deps.py` — FastAPI dependencies: `current_context()`, `require_auth()`.
- Use `respx` to mock the IdP in tests. No live Keycloak in unit tests.

References: ADR-0001, architecture §5.2, §7.

### 1.2 ODRL evaluator core (no inheritance yet)
- `policy/model.py` — Pydantic / dataclass models for the FDP ODRL profile (Offer, Permission, Prohibition, Action, supported Constraint types).
- `policy/parser.py` — parse a graph of `odrl:Offer` triples into the model. Reject anything outside the profile with a clear error pointing at the offending triple.
- `policy/evaluator.py` — pure-function evaluator: given a parsed Offer and a `RequestContext` plus a requested action, return `Decision` (PERMIT/DENY) and the rule that fired.
- Conflict resolution: deny wins by default, overridable per policy.
- Exhaustive unit tests with hand-rolled Offers covering every supported constraint type and conflict scenario.

References: ADR-0006, architecture §8.

### 1.3 Authorization cache and PDP wiring
- `policy/cache.py` — SQLAlchemy model for the `authz_index` table; repository methods for upsert and bulk lookup.
- `policy/pdp.py` — public `authorize(subject, action, resource)` and `authorized_graphs(subject, action)` functions that read from the cache and lazily populate it.
- Invalidation hooks: synchronous on policy write (called by metadata module), asynchronous on user role change.
- Integration tests using real Postgres via testcontainers.

References: architecture §8.5, §9.4.

---

## Phase 2 — Metadata provider with LDP

### 2.1 RDF graph CRUD via triple store adapter
- `metadata/graphs.py` — typed helpers for the per-record / per-meta / per-audit graph URI conventions.
- `metadata/repository.py` — `get_graph`, `put_graph`, `patch_graph` (apply SPARQL Update scoped to one graph), `delete_graph` plus the meta-metadata updates.
- ETag computation: canonicalize triples to N-Triples sorted, hash with BLAKE2b.

References: architecture §6, ADR-0007.

### 2.2 SHACL validation pipeline
- `metadata/shacl.py` — wraps pySHACL with a fast-path for cached compiled shapes.
- `validate_against(graph, shape_iri)` returning a structured violation report or success.
- Profile bootstrap pre-compiles known shapes; runtime falls back to compile-on-first-use.

References: architecture §10.1, §13 (server-side validation).

### 2.3 LDP server skeleton
- `metadata/ldp/router.py` — FastAPI router with GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS for resources and containers.
- Content negotiation across Turtle, JSON-LD, RDF/XML, N-Triples.
- ETag + `If-Match` for concurrency control.
- Link headers advertising LDP types.
- Per-method PEP calls into `policy.authorize`.

References: ADR-0008, architecture §10.

### 2.4 PATCH via SPARQL Update
- Parse the body with RDFLib.
- Authorize as `odrl:modify` on the target graph (implicit from URL).
- Apply to a virtual copy, run SHACL against the post-update state.
- On success, commit + update meta-metadata + emit `record.modified` event.
- Reject with 422 + violation report on SHACL failure; reject with 403 on policy denial.

References: architecture §10.3.

### 2.5 Meta-metadata management
- `metadata/meta.py` — generates the meta-metadata graph on create and updates it on every modification.
- Validate the meta-metadata graph against the configured meta-metadata schema on every write.

References: architecture §6.2.

---

## Phase 3 — SPARQL endpoint

### 3.1 Query parser and classifier
- `access/parser.py` — parse with RDFLib's algebra; classify as read/update; enumerate referenced graphs (FROM, FROM NAMED, GRAPH clauses, WITH for updates).
- Reject SERVICE clauses with 400 + clear message.
- Reject anonymous updates with 401.

### 3.2 Query rewriter
- `access/rewriter.py` — inject `FROM NAMED <g>` for each graph in the user's authorized read set, intersected with any explicit references.
- For updates: validate explicit targets against `authorized_graphs(subject, "modify")`. Reject ambiguous-target updates with 400 + remediation hint.

### 3.3 SPARQL endpoint API
- `access/router.py` — FastAPI router at `/sparql` accepting GET and POST, all standard result formats.
- Stream large CONSTRUCT/DESCRIBE results.

References: ADR-0004, architecture §9.

---

## Phase 4 — Metrics

### 4.1 Anonymization layer
- `metrics/anonymize.py` — pure function that transforms a raw event (with IP, identity, UA, query text) into an aggregate-safe event (country/region/city, daily visitor hash, event type, resource id, timestamp bucket). Drops everything else.
- Daily salt rotation in memory with monotonic clock.
- Property-based tests verifying the function never emits an identifying field downstream.

### 4.2 Event-bus subscriber and aggregation
- `metrics/pipeline.py` — subscribes to the in-process event bus through `anonymize`, writes raw events to a short-retention Postgres table.
- Hourly rollup job (arq) condenses raw → hourly → daily.

### 4.3 Dashboard API
- `metrics/api.py` — read-only endpoints serving the client's dashboard charts. Stewards see their resources; admins see system-wide.

References: ADR-0002, architecture §11.

---

## Phase 5 — Simple data provider and profile bootstrap

### 5.1 Data provider for open-access distributions
- `data/router.py` — serves files via `downloadURL` (stream or redirect per config) and exposes per-distribution scoped SPARQL endpoints via `accessURL`.
- v1 serves only distributions whose Offer permits anonymous read.

References: architecture §5.6.

### 5.2 Profile bootstrap
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
