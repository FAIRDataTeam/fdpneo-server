# ADR-0007: One named graph per metadata record

**Status:** Accepted
**Date:** 2026-05-18

## Context

Metadata records in the FDP are RDF. They live in a triple store that supports named graphs. The architecture must choose how records map onto graphs.

Two reasonable mappings:

1. **One graph per record.** Each record's triples are stored in a dedicated named graph identified by the record's URI.
2. **Shared/default graph.** Triples for many records coexist in the default graph or in coarser groupings (one graph per catalog, one per dataset type).

The choice has wide reach: it affects access control, partial updates, deletion semantics, and consistency.

## Decision

Every metadata record lives in exactly one named graph. The graph URI is deterministically derived from the record's URI. Two sibling graphs accompany each record graph: a meta-metadata graph at `<record-uri>/meta` and an audit graph at `<record-uri>/audit`.

The same invariant applies to LDP containers, SHACL schemas, and ODRL Offers — each is a named graph identified by its URI.

## Alternatives considered

**Shared default graph.** Rejected. Access control becomes a per-triple problem rather than a per-graph problem. Deletion becomes a graph-pattern problem rather than a graph drop. LDP `PATCH` cannot map onto SPARQL Update without first identifying which triples belong to which record. None of this is impossible, but all of it adds complexity to operations that should be cheap and obvious.

**One graph per catalog, with records sharing a graph.** Rejected for the same reasons at smaller scale.

**Use blank-node containment for record structure.** Rejected. Blank nodes are local to a graph; they cannot be referenced from outside; they break content-negotiation round-trips.

## Consequences

**Easier:**
- Per-record access control reduces to set membership over graph URIs. The PDP returns "this user can read these graphs" and SPARQL `FROM NAMED` constrains the dataset accordingly.
- LDP `PATCH` maps directly onto SPARQL Update implicitly scoped to the resource's graph.
- Replace and delete are graph-level operations: drop the graph or replace its contents atomically.
- Per-record provenance (meta-metadata) and per-record audit have natural homes in sibling graphs.

**Harder:**
- Records cannot share triples. A statement like "Dataset X is part of Catalog Y" is asserted from the dataset's side (`<X> dct:isPartOf <Y>` in the dataset's graph). If we also want the reverse navigation visible from the catalog's perspective, it has to be asserted on the catalog's side too. The redundancy is acceptable because the LDP membership triples handle most of this already.
- Cross-graph reasoning is harder. SPARQL queries that span multiple records need to enumerate the graphs (which the materialized authorization index already does for access control purposes).
- Triple store performance under many graphs varies by backend. GraphDB and Fuseki handle this well at FDP scales; Oxigraph and others have been validated for development use.

**Required of operators:**
- Choose a triple store with good named-graph support. The recommended defaults all qualify.
