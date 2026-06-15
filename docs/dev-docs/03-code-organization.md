# 3. Code Organization

This is the practical map: where every kind of code lives, the naming conventions, and a "I want to change X → start here" lookup. Keep it open while you work.

← [Application architecture](02-application-architecture.md) · Next → [Request lifecycle](04-request-lifecycle.md)

---

## 3.1 Repository layout

```
src/fdp/
├── main.py            Composition root: create_app(), middleware + router wiring, lifespan
├── config.py          Pydantic Settings (env-driven) — all configuration
├── cli.py             The `fdp` CLI (profile apply, pid, …)
├── operational.py     Health / readiness / info routers
│
├── identity/          OIDC, RequestContext, API keys, user-management facade
├── metadata/          The big one — see §3.3
├── policy/            ODRL evaluator, PDP, authorization cache
├── access/            SPARQL endpoint: parser → rewriter → adapter
├── data/              Simple data provider (distributions)
├── metrics/           Anonymous event pipeline, aggregation, dashboard API
├── storage/
│   ├── triplestore/   The SPARQL 1.1 adapter (the ONLY path to RDF)
│   └── postgres/      SQLAlchemy models, engine, custom types
└── shared/            Kernel: namespaces, events, context, errors, logging, RDF + safety utils

tests/
├── unit/              Fast, no I/O. Most tests live here.
├── integration/       testcontainers: real GraphDB/Postgres. Cross-module flows.
├── contract/          OpenAPI conformance
└── conformance/       FDP specs + LDP test suite

docs/
├── architecture/      The formal architecture document
├── adr/               14 ADRs (the "why")
└── dev-docs/          ← you are here

profiles/default/      The bundled DCAT deployment profile (schemas, offers, manifest)
deploy/                compose.yaml (dev stack) + helm/ (production)
```

## 3.2 How a context is structured internally

Most contexts follow the same internal shape, so once you learn one you can navigate them all:

| File pattern | Role |
|---|---|
| `router.py` | FastAPI `APIRouter`, built by a `build_*_router(...)` factory. The HTTP edge. |
| `*_service.py` / `service.py` | Application logic, stateless, shares collaborators. |
| `repository.py` | Persistence access (triple store or Postgres) for this context. |
| `model.py` | Internal domain types (dataclasses/attrs — **not** Pydantic). |
| `events.py` | Event types this context publishes/consumes. |
| `__init__.py` | The context's public surface. Import from here, not from internals. |

Routers are always **factory functions** (`build_x_router(*, service=...)`) rather than module-level singletons, so `create_app()` can inject collaborators and tests can build isolated instances.

## 3.3 Inside `metadata/` — the largest context

`metadata/` is ~60% of the code, so it has sub-packages. A guided tour:

```
metadata/
├── ldp/               The LDP server: router.py (GET/HEAD/PUT/POST/PATCH/DELETE), negotiation.py
├── repository.py      get_graph / put_graph / delete_graph — record-level RDF access
├── shacl.py           ShaclValidator: SHACL validation + shape-closure assembly (composition!)
├── shape_provider.py  Resolves shape IRIs to Turtle for the validator
├── schemas.py         Runtime SHACL shape admin (PUT /schemas/{id})
├── meta.py / audit.py Meta-metadata and audit (ODRL Agreement) graphs
├── containment.py     LDP container membership maintenance
├── lifecycle.py       Publication state machine (draft/published/…)
├── extensions.py      Non-LDP HTTP extensions: /spec, /expanded, /page
├── identifiers.py     Inbound IRI canonicalization + client-supplied identifier reconciliation
├── patch.py           SPARQL-Update PATCH simulation
├── profiles/          Deployment profile parsing, applying, bootstrap, resource definitions
├── search/            Full-text search: indexer (event-driven), service, repository
├── pid/               Persistent identifiers: w3id.py, github.py, verify.py, rebase.py
├── rd_api.py          Resource-definition admin API (ADR-0009)
├── policies.py        ODRL Offer/policy documents as managed records (ADR-0012)
├── licenses.py        Managed license documents
├── settings.py        Runtime settings store
├── dashboard.py       Metrics dashboard composition
└── autocomplete.py    Autocomplete over indexed labels
```

If `metadata/` feels like a lot, that's because it is — see the boundary note in [doc 2 §2.2](02-application-architecture.md#22-the-contexts-and-their-allowed-imports).

## 3.4 "I want to change X → start here"

| You want to… | Start in | Then probably |
|---|---|---|
| Change how a record is read/written over HTTP | [metadata/ldp/router.py](../../src/fdp/metadata/ldp/router.py) | `repository.py`, `meta.py` |
| Change SHACL validation or schema **composition** | [metadata/shacl.py](../../src/fdp/metadata/shacl.py) | `shape_provider.py`, the profile shapes in `profiles/default/schemas/` |
| Add/change a metadata **type** (resource definition) | [metadata/rd_api.py](../../src/fdp/metadata/rd_api.py) | [ADR-0009](../adr/0009-runtime-resource-definitions.md) |
| Change an access-control decision | [policy/evaluator.py](../../src/fdp/policy/evaluator.py), `pdp.py` | `cache.py`, [ADR-0006](../adr/0006-odrl-profile-permission-prohibition.md) |
| Change SPARQL access / query rewriting | [access/rewriter.py](../../src/fdp/access/rewriter.py) | `parser.py`, `router.py`, [ADR-0004](../adr/0004-sparql-access-via-named-graph-projection.md) |
| Change what metrics are captured | [metrics/middleware.py](../../src/fdp/metrics/middleware.py), `pipeline.py` | `anonymize.py`, [ADR-0002](../adr/0002-anonymous-metrics.md) |
| Add a config option | [config.py](../../src/fdp/config.py) | wire it in `main.py` |
| Add an RDF prefix | [shared/namespaces.py](../../src/fdp/shared/namespaces.py) | never redefine prefixes in module code |
| Change the error envelope | [shared/errors.py](../../src/fdp/shared/errors.py) | error middleware in `main.py` |
| Change how the triple store is called | [storage/triplestore/adapter.py](../../src/fdp/storage/triplestore/adapter.py) | capability flags in `config.py` |
| Change a Postgres table | [storage/postgres/models.py](../../src/fdp/storage/postgres/models.py) | add an Alembic migration |
| Change bootstrap / a profile | [metadata/profiles/](../../src/fdp/metadata/profiles/), `profiles/default/` | [doc 5 §6](05-key-processes.md#6-profile-bootstrap) |

## 3.5 Conventions you must follow

These are enforced in review and by the CI quality gate. Full list in [CLAUDE.md](../../CLAUDE.md); the high-frequency ones:

- **Type hints everywhere.** Pyright runs in strict mode. No bare `Any` without a comment explaining why.
- **Async by default.** Routes, SQLAlchemy sessions, and the triple store adapter are async. No sync I/O on the request path.
- **Pydantic at the edge, dataclasses inside.** Pydantic validates untrusted input at the HTTP boundary; internal domain types are plain dataclasses/attrs. Don't propagate Pydantic models deep into a context.
- **SPARQL and SQL are parsed/parameterized, never interpolated.** Build SPARQL via RDFLib algebra; never f-string a URI or literal into a query. SQL is always parameterized. (See [shared/sparql_safety.py](../../src/fdp/shared/sparql_safety.py).)
- **Structured logging only.** `structlog` with bound request context. No `print`, no stdlib logging formatter.
- **Structured errors.** Every HTTP error carries a stable `code`, a human message, and a `docs_url`. Raise the typed errors in `shared/errors.py`.
- **Imports** follow ruff's isort defaults: stdlib, third-party, first-party. `shared` is the only first-party module any context may import freely.

## 3.6 Tooling

```bash
uv sync                                  # install/sync deps (uv, not pip)
uv run fastapi dev src/fdp/main.py       # dev server (auto-reload)
uv run alembic upgrade head              # run migrations
uv run fdp profile apply ./profiles/default
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest                            # full suite
```

The full command list and the dev-stack `docker compose` are in [doc 7](07-contributing.md) and [CLAUDE.md](../../CLAUDE.md).

---

← [Application architecture](02-application-architecture.md) · Next → [Request lifecycle](04-request-lifecycle.md)
