# ADR-0003: Fixed Postgres for operational state

**Status:** Accepted
**Date:** 2026-05-18

## Context

The FDP needs to persist operational state beyond what the triple store should hold: aggregated metrics, the materialized authorization index, background-job state, OIDC session bookkeeping, and a policy-decision audit log.

Plausible options:
1. Store everything operational in the triple store, in dedicated named graphs.
2. Store operational state in an embedded key-value store (RocksDB, SQLite).
3. Store operational state in a dedicated relational database that is part of the architecture.

The first option keeps the deployment to a single backing store. The second keeps deployment simple but limits operational queries. The third adds a service the operator must manage but provides a tool well-suited to time-series aggregates and structured operational queries.

## Decision

PostgreSQL 16+ is a fixed component of the architecture. It stores all operational state. The triple store stores only RDF metadata, schemas, ODRL policies, and audit graphs that are conceptually part of the knowledge graph.

## Alternatives considered

**Operational data in named graphs in the triple store.** Rejected. Time-series aggregates (metrics) are awkward to model in RDF and even more awkward to query efficiently in SPARQL. Mixing operational state into the knowledge graph means an administrator dumping the triple store gets a graph polluted with internal counters and session bookkeeping. The separation we want is a separation in fact, not just a separation in convention.

**SQLite or embedded KV.** Rejected. Multi-process operation (horizontal scaling of the API server behind a load balancer) requires a shared backing store. SQLite over a network filesystem is a known bad pattern; an embedded KV would force us back to a single replica.

**Pluggable operational storage.** Rejected. Treating the operational store as pluggable creates false flexibility — every operational feature would need to be designed for multiple backends, with multiple migration stories, multiple dialects. The cost is real; the benefit (theoretical operator choice) is not exercised in practice.

## Consequences

**Easier:**
- The triple store is conceptually clean — it contains only the knowledge graph.
- Time-series queries, aggregations, and indexed lookups for the authorization cache use the right tool.
- Single migration story (Alembic), single connection pool, single backup story for operational data.

**Harder:**
- Operators must deploy and manage one more service.
- We have committed to PostgreSQL specifically. Switching to a different relational store later would require migration work.

**Acceptable because:**
- PostgreSQL is universally available across cloud providers and on-premise deployments.
- The Postgres feature set we rely on (JSONB, listen/notify, basic SQL) is conservative enough that we are not locked into vendor specifics.
