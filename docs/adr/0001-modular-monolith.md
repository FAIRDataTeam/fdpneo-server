# ADR-0001: Modular monolith over microservices

**Status:** Accepted
**Date:** 2026-05-18

## Context

The FDP server has four architecturally distinct responsibilities: serving metadata records and schemas (metadata provider), enforcing access control (security enforcer), gathering usage metrics (metrics gatherer), and providing simple open data access (simple data provider). A natural-looking design partitions these into separate services that communicate over the network.

The current FDP reference implementation is monolithic. Some teams in the FAIR community have proposed splitting future implementations into independent services for scalability and team autonomy.

## Decision

The FDP v2 server is built as a **modular monolith**: a single deployable process containing four bounded contexts with clear code-level boundaries and explicit cross-component interfaces, sharing a single process, a single database connection pool, and a single triple store connection.

The four bounded contexts (metadata, security, metrics, data) communicate through:
- Explicit synchronous interfaces (Python protocols / abstract base classes) for request-time interactions
- An in-process event bus for asynchronous fan-out (e.g., metrics observing metadata events)

A shared kernel provides cross-cutting utilities (RDF parsing, namespace registry, request context, error types) that any component may import.

## Alternatives considered

**Microservices, one per bounded context.** Rejected. The four components share too many concerns at runtime — every request needs the same authentication context, the same policy decisions, the same RDF parsing, and most requests touch the same triple store. The cross-service communication would dominate execution. Operational overhead (four services to deploy, monitor, version, secure between) would exceed any scalability benefit, especially at the deployment scales typical for FDP (one organization, tens to thousands of records, modest query load).

**A single unstructured monolith.** Rejected. Without bounded contexts, the current reference implementation's legacy issues are likely to recur: cross-cutting code creep, unclear ownership of behavior, difficulty reasoning about what changes affect what.

**Service-oriented split only at the SPARQL endpoint.** Considered. The SPARQL endpoint plausibly has different scaling characteristics from the management API. Deferred: if production deployments show this is actually a bottleneck, the SPARQL endpoint module can be lifted into its own service later, since its boundaries are already clean.

## Consequences

**Easier:**
- Deployment is a single image, single process.
- Cross-component changes do not require API versioning between internal services.
- Local development requires only the application plus its two external dependencies (triple store, Postgres).
- Refactoring across components is straightforward.

**Harder:**
- Independent scaling of components is not possible without explicit work to split a component out. The architecture leaves this path open by maintaining clean module boundaries.
- A single deployment unit means a single failure domain. Acceptable for the FDP's reliability requirements.

**Requires discipline:**
- Bounded-context boundaries must be enforced in code review. Helper imports across contexts are red flags; they should be promoted to the shared kernel if truly cross-cutting, or kept inside the originating context otherwise.
