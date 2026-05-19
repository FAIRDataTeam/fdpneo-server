# ADR-0004: SPARQL access control via named-graph projection

**Status:** Accepted
**Date:** 2026-05-18

## Context

The FDP exposes a SPARQL endpoint. Some metadata records are public; others are restricted by ODRL policy. The endpoint must enforce policy correctly across the full SPARQL surface — `SELECT`, `ASK`, `CONSTRUCT`, `DESCRIBE`, aggregates, `OPTIONAL`, `MINUS`, nested patterns, `GRAPH` clauses, updates — without leaking information through cardinality, error messages, or timing.

Three plausible approaches exist.

## Decision

The FDP enforces SPARQL access control by **named-graph projection**: the server enumerates the user's authorized graph set, then injects `FROM NAMED <g>` clauses constraining the query's dataset to those graphs. SPARQL's dataset semantics handle the rest — the engine sees only the authorized graphs, so cardinalities, aggregates, joins, and `OPTIONAL` all behave correctly with no further intervention.

This decision presupposes [ADR-0007](0007-one-graph-per-record.md) (one named graph per record): the authorized graph set is set membership over record-graph URIs.

## Alternatives considered

**Result filtering (post-execution).** Run the query as written, drop disallowed rows from the result set. Rejected. `COUNT(*)` and other aggregations are computed before filtering, so they leak the cardinality of unauthorized data. Joins involving unauthorized records change the cardinality of authorized results, leaking existence. This is unsalvageable.

**`FILTER`-based query rewriting.** Inject `FILTER` clauses that constrain a graph variable to the authorized set. Rejected. The scope of `FILTER` interacts unintuitively with `OPTIONAL`, `MINUS`, and nested `SELECT`. Getting this right requires reasoning about every SPARQL construct in the algebra; getting it wrong leaks data silently. Even when it works, the engine still touches the unauthorized data internally, which has performance and side-channel implications.

**Per-graph proxy endpoints.** Maintain a separate URL for each authorization-relevant subset of graphs, and let users target the right URL. Rejected. Combinatorial explosion of subsets per (user, role-set), and users would not know which subset to target.

**Re-issue the query as the triple store's own user.** Some triple stores have their own ACL systems. Rejected. The FDP is portable across triple stores; we cannot rely on vendor-specific ACL features. We also do not want the triple store to know about FDP users.

## Consequences

**Easier:**
- Correctness across SPARQL's full surface falls out of dataset semantics. We do not need to enumerate every SPARQL construct.
- Performance is bounded by the size of the authorized graph set, which we materialize once per (user, action) pair in Postgres.
- The same enforcement mechanism handles LDP `PATCH` (which is a SPARQL Update implicitly scoped to one graph).

**Harder:**
- The model is tied to the one-graph-per-record invariant. Records cannot share triples; cross-record assertions live in one record's graph or are made redundantly.
- Updates targeting graphs implicitly (`DELETE { ?s ?p ?o } WHERE { ... }` without `WITH` or `GRAPH`) cannot be authorized without running the `WHERE` first. v1 restricts updates to explicit-target forms.

**Information-leakage rules:**
- A query that explicitly names an unauthorized graph (`FROM <g>`, `FROM NAMED <g>`, `GRAPH <g> { ... }`) returns 403 for that graph. The user revealed knowledge of the URI by typing it, so the response leaks nothing.
- A query that names no graphs is silently constrained. The user has no way to distinguish "no such record" from "you cannot see this record".
