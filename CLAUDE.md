# Claude Code context — fdp-server

This file is read automatically by Claude Code on every session. It establishes the project context and conventions so each session starts informed.

## What this project is

The FAIR Data Point (FDP) v2 server — a Python/FastAPI metadata repository implementing the [FDP specifications](https://specs.fairdatapoint.org), with a full LDP API, a SPARQL endpoint with access control, ODRL-based authorization, and anonymous-by-design usage metrics.

The client is a separate repository (`fdp-client`); changes that affect the API contract require coordinated work there.

## Read these first

Before doing substantive work, read:

1. [`docs/architecture/README.md`](docs/architecture/README.md) — full architecture, 15 sections. The most important reference.
2. [`docs/adr/`](docs/adr/) — 8 ADRs documenting the rationale for the controversial choices (one-graph-per-record, named-graph projection for SPARQL, anonymous metrics, ODRL profile scope, full LDP, etc.). When a design question arises, check whether an ADR already answers it.

These documents are authoritative. If something in the code disagrees with them, default to following the docs and flag the discrepancy rather than rewriting the docs to match the code.

## Architectural ground rules

The server is a **modular monolith** with four bounded contexts. Respect the boundaries:

| Module | Owns | Imports allowed |
|---|---|---|
| `identity/` | OIDC, request context | shared |
| `metadata/` | LDP server, records, schemas, SHACL | shared, storage |
| `policy/` | ODRL PDP, authorization cache | shared, storage |
| `access/` | SPARQL endpoint, query rewriting | shared, policy, storage |
| `data/` | Simple data provider | shared, policy, storage |
| `metrics/` | Anonymous event pipeline, dashboard API | shared, storage |
| `storage/` | Triple store adapter, Postgres repository | shared |
| `shared/` | Cross-cutting utilities | nothing |

**Cross-module communication is explicit.** No reaching into another module's internals. The `policy` module's interface is the `authorize(subject, action, resource)` protocol — every PEP calls through that. Async events go on the in-process event bus in `shared`.

**The shared kernel is for genuinely cross-cutting concerns only.** RDF utilities, namespace registry, event bus, identity context, error types, structured logging. If you're tempted to add something here, ask whether it really crosses contexts or whether it belongs inside one of them.

## Critical invariants

These are not negotiable without an ADR update:

- **One named graph per metadata record.** Every record's triples live in exactly one graph. Sibling graphs at `<record-uri>/meta` and `<record-uri>/audit` hold meta-metadata and ODRL Agreements. See [ADR-0007](docs/adr/0007-one-graph-per-record.md).
- **The triple store holds only the knowledge graph.** Metrics, audit hashes, auth cache, job state — all in Postgres. Never write operational state to named graphs.
- **The metrics pipeline never sees identifying data.** IPs, identities, user agents, query text are dropped before events reach the metrics handler. Anonymization happens at ingress, not at report time. See [ADR-0002](docs/adr/0002-anonymous-metrics.md).
- **All RDF I/O goes through the triple store adapter.** No direct vendor calls, no SPARQL HTTP outside the adapter port. Use SPARQL 1.1 Protocol.
- **All policy decisions go through the security enforcer.** Don't evaluate ODRL directly in another module.

## Tech stack

- Python 3.12+
- FastAPI for HTTP
- Pydantic v2 for edge validation
- RDFLib for RDF (Oxigraph bindings reserved for hot paths if needed)
- pySHACL for SHACL validation
- Authlib for OIDC
- SQLAlchemy 2.x async + Alembic for Postgres
- arq for background jobs (Postgres-backed, no Redis)
- structlog + OpenTelemetry for observability
- pytest + pytest-asyncio + testcontainers for tests
- uv for dependency management
- ruff for lint + format, pyright for types

## Common commands

```bash
# Bring up the dev stack (GraphDB, Postgres, Keycloak)
docker compose -f deploy/compose.yaml up -d

# Install / sync dependencies
uv sync

# Run migrations
uv run alembic upgrade head

# Apply the default deployment profile
uv run fdp profile apply ./profiles/default

# Start the API server in dev mode
uv run fastapi dev src/fdp/main.py

# Run tests
uv run pytest                          # full suite
uv run pytest tests/unit               # fast unit tests
uv run pytest tests/integration -m gh  # integration against GraphDB
uv run pytest -k policy                # filter by name

# Lint and format
uv run ruff check .
uv run ruff format .

# Type check
uv run pyright
```

## Code conventions

- **Type hints required everywhere.** Pyright runs in strict mode. No `Any` without a comment explaining why.
- **Async by default.** FastAPI routes, SQLAlchemy sessions, and the triple store adapter are all async. Don't introduce sync I/O on the request path.
- **Pydantic at the edge, dataclasses inside.** Use Pydantic for request/response models and for parsing untrusted input. Use plain dataclasses or attrs for internal domain types. Don't propagate Pydantic models deep into the modules.
- **Error envelope is structured.** Every error returned over HTTP includes a stable code, a human-readable message, and a docs URL. See `shared/errors.py`.
- **Logging is structured.** Use `structlog` with bound request context. Don't `print`, don't use the stdlib `logging` formatter directly.
- **Tests follow the pyramid.** Most tests are unit (fast, no I/O). Integration tests use testcontainers-launched GraphDB/Fuseki/Oxigraph and Postgres. Don't add slow tests to the unit suite.
- **SPARQL strings are parsed, never interpolated.** Use RDFLib's algebra to build queries; never f-string a URI or literal into a query. Same rule for SQL — always parameterized.
- **Imports follow ruff's isort defaults.** Stdlib first, then third-party, then first-party. The shared kernel is the only first-party module any context can import.

## RDF and namespaces

The namespace registry is in `shared/namespaces.py`. Add prefixes there; do not redefine them in module code. Common prefixes: `dcat:`, `dct:`, `foaf:`, `ldp:`, `odrl:`, `prov:`, `sh:`, `xsd:`.

## What not to do

- **Don't bypass the LDP layer for record CRUD.** Even internal helpers go through the LDP server's methods, so SHACL validation and meta-metadata update happen consistently.
- **Don't evaluate ODRL in the metadata module.** Call `policy.authorize(...)` and read the decision.
- **Don't store user identity in metrics tables.** If you're tempted to add a `user_id` column to a metrics table, stop. The anonymization boundary is structural.
- **Don't use a vendor-specific API on the triple store.** Vendor capabilities live behind capability flags. If you need something not expressible in SPARQL 1.1 Protocol, raise it in an ADR.
- **Don't add a new dependency without checking.** This codebase is intentionally conservative. RDFLib, pySHACL, Authlib, FastAPI, SQLAlchemy, and a small set of utilities are the core. New runtime deps need a justification.

## Repository layout

```
src/fdp/
├── identity/         OIDC, request context
├── metadata/         LDP server, records, schemas, SHACL
├── policy/           ODRL evaluator, PDP, authorization cache
├── access/           SPARQL endpoint, query rewriting
├── data/             Simple data provider
├── metrics/          Anonymous event pipeline, dashboard API
├── storage/          Triple store adapter, Postgres repository
├── shared/           Cross-cutting utilities
└── main.py           FastAPI app composition
tests/
├── unit/
├── integration/
├── contract/         OpenAPI conformance
└── conformance/      FDP specs and LDP test suite
deploy/
├── compose.yaml      Dev stack
└── helm/             Production
docs/
├── architecture/
└── adr/
profiles/
└── default/          Bundled DCAT default profile
```

## When working on a task

1. If the task touches a controversial area (SPARQL access control, ODRL evaluation, metrics privacy, the LDP layer, the storage adapter), re-read the relevant ADR before changing code.
2. Plan the change at the module level first. Identify which bounded context owns the change. If the answer is "more than one", that's a smell — re-think.
3. Write or update tests in the appropriate layer. Unit tests for logic, integration tests for cross-module flows.
4. Run the full quality gate before declaring done: `uv run ruff check . && uv run pyright && uv run pytest`.
5. If the change affects the public API, the OpenAPI spec must update, and the client repository will need a coordinated change to regenerate types.

## Open questions and known gaps

The architecture document's [Section 15](docs/architecture/README.md#15-open-questions) lists open questions. The biggest live ones:

- IdP role-to-FDP-role mapping config (deferred to v1.x)
- SPARQL update restriction ergonomics (revisit after community feedback)
- Policy-decision audit-log default (currently on)
- LD-PATCH support (currently SPARQL Update PATCH only)

When you encounter a gap that isn't covered by the docs or ADRs, document the choice you made in the PR and consider whether it warrants a new ADR.
