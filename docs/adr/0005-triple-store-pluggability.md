# ADR-0005: Pluggable triple store via SPARQL 1.1 Protocol

**Status:** Accepted
**Date:** 2026-05-18

## Context

The FDP stores its metadata as RDF in a triple store. Different deployments have different constraints: some have organizational standards (a specific vendor in use), some prefer fully open-source stacks, some want a lightweight embedded option for development. The current FDP reference implementation supports multiple triple store backends.

The FDP server interacts with the store for: querying records, writing/updating/deleting record graphs, ingesting bulk data during profile bootstrap, and serving the SPARQL endpoint by proxying queries.

## Decision

The FDP communicates with the triple store **exclusively through SPARQL 1.1 Protocol**. A single triple store adapter port exposes a small set of operations (query, update, ingest_graph, replace_graph, drop_graph) implemented as SPARQL Protocol calls. Operators choose the backend at deploy time by configuring the SPARQL endpoint URL.

The FDP ships with tested adapter configurations for three backends:

- **GraphDB** (recommended default) — strong administrative UI, good performance for typical FDP scales.
- **Apache Jena Fuseki** — fully open source, widely deployed.
- **Oxigraph** — lightweight, easy for development and small deployments.

Vendor-specific capabilities (GraphDB repository management, named-graph cluster synchronization, vendor-proprietary reasoners) sit behind capability flags. The base adapter does not require any backend to implement them.

## Alternatives considered

**Coupling to a specific triple store.** Rejected. Loses the portability the current implementation has. Some deployments cannot adopt a specific vendor.

**Per-backend adapters using each vendor's native API.** Rejected. SPARQL 1.1 Protocol is sufficient for everything the FDP needs at the data layer; using vendor APIs would multiply adapter code without functional benefit and would couple FDP features to vendor release schedules.

**Embedded triple store (Oxigraph in-process).** Considered. Oxigraph's Python bindings make this feasible. Deferred: useful as a default for zero-config development setups, but not as the only mode — production deployments need the option of an external store for operational reasons (separate backup, separate scaling).

## Consequences

**Easier:**
- Operators choose the backend; FDP code is portable.
- Adapter is small (translates a stable set of operations to SPARQL HTTP calls).
- Adding a new backend means writing a small adapter and validating it against the conformance suite.

**Harder:**
- Performance characteristics differ across backends. The FDP performance guidance is tied to the recommended backend (GraphDB); operators choosing other backends accept some variability.
- Features that would benefit from vendor-specific optimizations (full-text search, geospatial query) require fallback implementations or feature flags. v1 does not depend on any.

**Recommended default:**
- GraphDB is the recommended default for production. Oxigraph for development. Fuseki for deployments that prefer open-source stacks.
