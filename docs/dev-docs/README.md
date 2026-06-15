# FDP Server — Developer Documentation

Welcome. This package exists to take you from "I just cloned the repo" to "I can land a change that respects the architecture" as fast as possible. It is written for a new engineer who knows Python and HTTP but has never seen this codebase.

It complements, and does not replace, two existing sources of truth:

- [`docs/architecture/README.md`](../architecture/README.md) — the formal 15-section architecture document.
- [`docs/adr/`](../adr/) — 14 Architecture Decision Records explaining *why* the controversial choices were made.

When this developer guide and those documents disagree, **the architecture doc and the ADRs win** — tell us so we can fix the guide.

## What the FDP server is, in two sentences

The FAIR Data Point (FDP) v2 server is a Python/FastAPI metadata repository. It exposes a W3C Linked Data Platform API over an RDF knowledge graph, with a query-layer-access-controlled SPARQL endpoint, ODRL-based authorization, and anonymous-by-design usage metrics.

## How to read this package

Read in order. Each document assumes the previous ones.

| # | Document | What you learn | Read when |
|---|---|---|---|
| 1 | [System context](01-system-context.md) | Who uses the FDP, what it talks to, the big-picture containers | First. 10 min. |
| 2 | [Application architecture](02-application-architecture.md) | The modular monolith, the seven bounded contexts, the import rules, the event bus | Before touching any module. |
| 3 | [Code organization](03-code-organization.md) | Where every kind of code lives, naming conventions, "I want to change X → go here" | When you start coding. |
| 4 | [Request lifecycle](04-request-lifecycle.md) | The middleware pipeline and routing every HTTP request flows through | When debugging a request. |
| 5 | [Key processes](05-key-processes.md) | The seven core flows (record CRUD, authorization, SPARQL access, schema validation, metrics, bootstrap, PIDs) with sequence + activity diagrams | The heart of the system. |
| 6 | [Data model](06-data-model.md) | One-graph-per-record, meta/audit graphs, reserved namespaces, what lives in Postgres vs the triple store | When your change touches storage. |
| 7 | [Contributing](07-contributing.md) | Dev setup, the quality gate, the testing pyramid, how to add a feature without breaking a boundary | Before you open a PR. |

## The five-minute orientation

If you only have five minutes, internalize these five facts. The rest of the package expands each one.

1. **It's a modular monolith with hard internal boundaries.** Seven bounded contexts (`identity`, `metadata`, `policy`, `access`, `data`, `metrics`, `storage`) plus a `shared` kernel. Modules talk through explicit interfaces, not by reaching into each other. See [doc 2](02-application-architecture.md).

2. **One named graph per record.** Every metadata record's triples live in exactly one named graph in the triple store, with sibling `…/meta` and `…/audit` graphs. This is the load-bearing invariant. See [doc 6](06-data-model.md) and [ADR-0007](../adr/0007-one-graph-per-record.md).

3. **Two stores, strict split.** The triple store holds the knowledge graph (and the FDP's own RDF config records). Postgres holds operational state — metrics, auth cache, job state, OIDC bookkeeping. Never cross the streams. See [ADR-0003](../adr/0003-fixed-postgres-for-operational-state.md).

4. **Every access decision goes through the PDP.** `policy.authorize(subject, action, resource)` is the one door. The metadata LDP layer and the SPARQL endpoint are both Policy Enforcement Points that call it. See [doc 5 §2](05-key-processes.md#2-authorization-pdppep).

5. **The metrics pipeline never sees identifying data.** Anonymization happens at ingress, structurally, before events reach the metrics handler. See [ADR-0002](../adr/0002-anonymous-metrics.md).

## The API lives under `/fdp-api`

A gotcha worth knowing on day one: all the JSON/admin API surface (OpenAPI, `/spec`, `/schemas`, search, metrics, SPARQL) is mounted under the **`/fdp-api`** reserved prefix. The root path space (`/`, `/catalog/{id}`, …) is the **LDP record space**, served by a catch-all that is registered last. So `GET /catalog/dc-check` returns a *record*; `GET /fdp-api/catalog/spec` returns that type's *SHACL shape*. See [doc 4](04-request-lifecycle.md).

## Conventions used in the diagrams

- Diagrams are [Mermaid](https://mermaid.js.org) — they render in GitHub, GitLab, VS Code (with a Mermaid extension), and most Markdown viewers.
- **ArchiMate-style** views are drawn as labelled Mermaid graphs (ArchiMate's native `.archimate` format is not text-diffable, so we approximate its layered views and tag each layer).
- **UML Sequence** and **UML Activity** diagrams use Mermaid's native `sequenceDiagram` and `flowchart` support.
