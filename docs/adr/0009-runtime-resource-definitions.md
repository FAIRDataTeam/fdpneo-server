# ADR-0009: Runtime-mutable resource definitions stored as RDF

**Status:** Accepted
**Date:** 2026-05-30

## Context

A *resource definition* (RD) is the FDP's description of one metadata type it exposes: a URL prefix (route segment), the SHACL shape that validates instances of that type, and the typed child links that relate it to other types (a Catalog has Datasets via `dcat:dataset`, and so on). The set of RDs drives two runtime surfaces:

- the LDP container registry — which URLs are typed collections, which shape validates a POST/PUT/PATCH, where members live; and
- the OpenAPI generator — the per-type path set (`/{prefix}`, `/{prefix}/{id}`, `/spec`, `/expanded`, `/page/{childPrefix}`) and its `Metadata: <Name>` tags.

In the initial implementation, RDs are derived **once, at bootstrap**, from the deployment profile manifest's `resourceDefinitions:` list, materialized into an immutable in-memory `ResourceDefinitionCache`, and never changed thereafter (`metadata/profiles/registry.py`: *"Live mutation … is out of v1 scope"*). Exposing a new type — the canonical example: a community defines an `Ontology` schema and wants Catalogs to also hold Ontology records — therefore requires editing the profile bundle and a full re-bootstrap, which under architecture §12.3 wipes the stores. That is too blunt for what is a routine curation act.

The reference implementation (FAIRDataPoint, Java/Spring) treats RDs as first-class, runtime-CRUD-able entities: `ResourceDefinitionService.create/update/delete` persists the change, recomputes its caches, updates membership, and calls `OpenApiService.updateGenericPaths()` / `removeGenericPaths()` so the documented API surface tracks the change live. Routing is a generic catch-all that resolves the RD from the URL per request.

This FDP's HTTP layer is already shaped the same way — a single `/{path:path}` LDP router and a `_DynamicContainerRegistry` that reads `app.state.resource_definitions` on **every** call, plus an idempotent OpenAPI injector. The machinery to make new types appear without a router rebuild exists; what is missing is (a) a place to keep RDs that survives a restart and can change at runtime, (b) a mutation surface, and (c) the decision about *where* RDs are stored, given two hard constraints already in force:

- **ADR-0003 / architecture §4.4** — operational state lives in Postgres; the triple store stays a pure metadata repository.
- **ADR-0005 / architecture §4.3** — all RDF I/O goes through one SPARQL 1.1 Protocol adapter; vendor capabilities (e.g. GraphDB repository management) sit behind capability flags and must not be required for core function.

The deployment also needs RD changes to be invisible to ordinary knowledge-graph consumers: a SPARQL query or search over the metadata must not return RD records.

## Decision

**1. Resource definitions become runtime-mutable.** A steward/administrator may create, edit, and delete RDs and their child links at runtime through an admin API, and the LDP + OpenAPI surfaces update without a restart and without re-bootstrap. Re-bootstrap remains required only to change the bootstrap *profile* itself; architecture §12.3 is unchanged for that case. The profile manifest's `resourceDefinitions:` becomes the **seed** for the RD set, not its permanent definition.

**2. Resource definitions are stored as RDF records, in the same triple store as the knowledge graph.** Each RD is one record in its own named graph (honouring ADR-0007's one-graph-per-record invariant) under a reserved admin IRI namespace, `<base_url>/resource-definitions/{id}`. RDs are written through the LDP/metadata layer like any other record, so they inherit meta-metadata (creator, created, modified, version), ETags, content negotiation, and SHACL validation against a predefined RD shape (see ADR follow-up tasks). An RD record carries `fdp:urlPrefix`, a name, `ldp:constrainedBy` pointing at its instance shape, and one child-link node per child relation. The in-memory `ResourceDefinitionCache` is demoted to a rebuildable *projection* of these records; the triple store is the single source of truth.

**3. Storage is a separate named-graph namespace, not a separate repository.** RDs live alongside the knowledge graph in one repository, isolated by IRI namespace rather than by a distinct triple-store repository/dataset.

**4. A single, structural internal-graph exclusion governs visibility.** There is one shared set of reserved *internal* graph-URI patterns — `…/meta`, `…/audit`, and the `…/resource-definitions/` namespace (and any future admin data) — and all internal-graph filtering routes through it:

- the SPARQL endpoint's dataset construction / query rewriter (architecture §9.3) always excludes these from the public/default dataset; and
- the materialized authorization index / PDP never grants `read` on them to non-admin subjects.

The existing ad-hoc exclusion of `meta`/`audit` graphs (architecture §6.3) is refactored to use this same set, so there is exactly one definition and one test surface for "what counts as internal".

**5. The UX is explicit and two-step (Option A).** Describing a type (authoring a SHACL shape) and exposing a type (registering an RD that points at that shape and places it under a parent) are distinct acts. Publishing a shape does not auto-expose it. RD creation validates that the referenced shape already exists and parses. This matches the reference implementation's separation of schemas from resource definitions and keeps draft/helper shapes from leaking into the URL space.

## Alternatives considered

**Keep RDs bootstrap-only (status quo).** Rejected. Adding a type is a normal curation need; forcing a destructive re-bootstrap for it is disproportionate and loses all records.

**Store RDs in Postgres** (as the reference implementation stores them in MongoDB). Rejected. It would split the "what types exist" definition across two stores, require a bespoke table + repository + migration, and lose the free benefits of the LDP layer (versioned meta-metadata, ETags, content negotiation, and pickup by `fdp profile export`). RDs are metadata *about the structure of* the knowledge graph, not operational noise like metrics, auth-cache rows, or job state — see the invariant discussion below.

**Store RDs in a separate triple-store repository/dataset.** Rejected. A second repository gives hard physical isolation but is not expressible in SPARQL 1.1 Protocol — creating and managing repositories is a vendor-specific capability (GraphDB repositories, Fuseki datasets, embedded Oxigraph's single store), exactly the kind of thing ADR-0005 puts behind a capability flag. It would force two configured endpoints, two connection pools, two backup/restore targets, a fatter adapter port (every operation must pick a target), and per-backend provisioning at bootstrap. The isolation it buys is largely redundant here: the SPARQL endpoint is not a passthrough — every query is parsed and rewritten to a dataset built from the caller's authorized graph set (ADR-0004, §9.3), so internal graphs are excluded at the query layer regardless of physical layout. A separate repository would only protect against a rewriter/dataset-builder bug — and that same bug would already expose `meta`/`audit` graphs, so the filter has to be correct anyway. One well-tested exclusion beats two storage topologies.

**Auto-expose a type when its SHACL shape is published (Option B).** Rejected as the default. The interesting parts of an RD — the URL prefix, the parent, and the child-link predicate that places the type in the hierarchy — have no natural home in a SHACL shape, so Option B needs a side channel anyway, and it turns every draft or `sh:node`-reused helper shape into a public URL space. Option A keeps "describe" and "expose" cleanly separable. A future convenience (suggest an RD when a shape is published, requiring explicit confirmation) is compatible with this decision.

## Consequences

**Easier:**

- Communities add types (e.g. `Ontology`) and re-parent them (Catalog → Ontology) at runtime through the admin API; endpoints and OpenAPI light up with no restart, because the router and OpenAPI injector already read the cache per-call and the mutation path swaps the cache + clears the cached spec.
- RDs get versioning, provenance, ETag concurrency, and export for free by being ordinary LDP records.
- Internal-graph visibility has a single structural definition shared by access control and SPARQL projection, shrinking the surface where a leak could be introduced and giving one place to test it.
- Backups and `fdp profile export` capture RDs naturally — they are in the same store as everything else.

**Harder:**

- The "triple store holds only the knowledge graph" rule (architecture §4.4, ADR-0003) gains a deliberate nuance: it holds the knowledge graph **and** the FDP's own metadata/configuration records (RDs, schemas, offers — all already RDF), but still **not** operational state (metrics, authorization-index rows, job state, OIDC bookkeeping), which remains in Postgres. The distinction is "is it metadata describing the repository's content/structure?" (triple store) versus "is it runtime bookkeeping?" (Postgres). This ADR makes that line explicit; §4.4 is updated to say so.
- Correctness now depends on the internal-graph exclusion being applied everywhere a dataset is built. This is mitigated by making it a single shared set rather than per-call logic, and by the conformance/integration tests that assert RD/meta/audit graphs never appear in public SPARQL results or anonymous reads.
- A consistency window exists between an RD mutation and the cache swap, same class of issue as the authorization index (§9.4); the swap is synchronous on the write path and the OpenAPI spec is rebuilt lazily on next request.

**Required of operators:**

- Nothing new at the infrastructure level — no second repository, no extra endpoint. RD curation is gated by the same ODRL/role machinery as other administrative actions.

## Related decisions

- [ADR-0003](0003-fixed-postgres-for-operational-state.md) — operational state in Postgres; this ADR clarifies the metadata-vs-operational line.
- [ADR-0004](0004-sparql-access-via-named-graph-projection.md) — named-graph projection is what makes named-graph isolation sufficient.
- [ADR-0005](0005-triple-store-pluggability.md) — SPARQL-1.1-only is why a second repository is rejected.
- [ADR-0007](0007-one-graph-per-record.md) — one graph per record, applied here to RD records.
- [ADR-0008](0008-full-ldp-with-patch.md) — the LDP layer RD records are written through.
