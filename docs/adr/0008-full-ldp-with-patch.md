# ADR-0008: Full LDP implementation including PATCH

**Status:** Accepted
**Date:** 2026-05-18

## Context

The current FDP reference implementation uses only the containment portion of the W3C Linked Data Platform specification. CRUD on records is implemented through a hand-rolled REST API. This means clients have to fetch a whole record, modify a property, and re-submit the whole record to make a small change — wasteful and racy.

LDP defines a complete, standard API surface for RDF resources, including `PATCH` for partial modifications. Implementing it fully removes the need for a custom CRUD API and gives clients predictable semantics aligned with broader semantic-web tooling.

## Decision

The FDP implements W3C LDP fully:

- **Containers.** Direct Containers; the FDP hierarchy (Repository → Catalogs → Datasets → Distributions) maps to nested containers. Each container links via `ldp:constrainedBy` to the SHACL schema its members must satisfy.
- **Resources.** Every record, schema, and policy is an LDP RDF Source. Content negotiation across Turtle, JSON-LD, RDF/XML, N-Triples.
- **Methods.** `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`. ETag plus `If-Match` for concurrency control.
- **PATCH.** Body is `application/sparql-update`, implicitly scoped to the resource's graph.

LD-PATCH (`application/ldpatch`) is **not** supported in v1. SPARQL Update PATCH is sufficient, reuses the existing SPARQL pipeline, and is more widely understood by clients.

## Alternatives considered

**Keep the current partial LDP support and extend the custom REST API.** Rejected. The custom API duplicates what LDP already specifies, with worse interoperability. Every new RDF-aware client has to learn the FDP's particular CRUD conventions.

**JSON:API or similar non-RDF-native REST style.** Rejected. The FDP is RDF-native at its core. An RDF-native API removes an entire layer of impedance mismatch.

**LD-PATCH instead of SPARQL Update PATCH.** Considered. LD-PATCH is more JSON-LD-friendly. Rejected for v1 because SPARQL Update is more universally supported by RDF client libraries and reuses the same parser, classifier, and execution path as the SPARQL endpoint. LD-PATCH could be added in v1.x as an additional accepted content type without architectural impact.

**Implement only `GET`, `POST`, `PUT`, `DELETE` and skip `PATCH`.** Rejected. The user-stated requirement is precisely that the user "could submit to just add values of some property of an existing metadata record without having to retrieve the whole record". `PATCH` is the answer to that.

## Consequences

**Easier:**
- Clients use a standard, predictable API. RDF-aware client libraries work without FDP-specific adaptation.
- Partial updates are first-class. Clients add a keyword, change a description, or update a single field without round-tripping the whole record.
- `PATCH` and the SPARQL endpoint share the same parser, classifier, and authorization pipeline. The difference is that `PATCH`'s target graph is fixed by the resource URL.
- Concurrency control via ETag plus `If-Match` is essentially free.

**Harder:**
- Authorization for `PATCH` requires the same care as for SPARQL updates: parse, classify, authorize, validate post-update state against SHACL, then commit atomically.
- The metadata module must simulate the update before committing, to run SHACL against the post-update state. This is the cost of correctness; the alternative (commit then validate then roll back) is worse for concurrency.

**Property-level access control is not in scope.** If a user has `odrl:modify` on a record, they can `PATCH` any property in it. Going finer-grained is a v2 concern (see main architecture, Section 10.4).

## Implementation status (v0.2, TASKS 15.1)

The "Direct Containers" decision is now real. As of v0.2 the FDP is an LDP Direct
Container server, and the conformance position is recorded — requirement by
requirement — in the [LDP conformance matrix](../conformance/ldp-conformance.md).
Summary:

- **Every FDP container is a genuine `ldp:DirectContainer`,** not an
  `ldp:BasicContainer`. The membership configuration (`ldp:membershipResource` =
  the container, one `ldp:hasMemberRelation` per resource-definition child link,
  `ldp:insertedContentRelation ldp:MemberSubject`) is derived by
  `applier.direct_container_config` and applied uniformly:
  - the **root (FAIRDataPoint)** carries it from the profile seed;
  - **runtime container records** (catalogs/datasets) are stamped on create
    (`PUT`/`POST`) by the LDP router (`_stamp_container_config`);
  - **deployments bootstrapped before 15.1** are reconciled non-destructively by
    `fdp ldp backfill-membership` (`metadata/profiles/backfill.py`), which adds
    the config to existing containers and strips the stale `ldp:BasicContainer`
    type without bumping record versions.
- **Headers reflect the model:** `Link: rel="type"` advertises
  `ldp:Container` + `ldp:DirectContainer` for container endpoints, `Accept-Post`
  is container-only, `Accept-Patch` is `application/sparql-update`, and reads
  advertise the SHACL shape via `Link: rel="…ldp#constrainedBy"`.
- **An automated conformance suite** ([`tests/conformance/test_ldp.py`](../../tests/conformance/test_ldp.py))
  encodes the load-bearing MUST/SHOULD rows as HTTP-level checks against a real
  triple store; all pass on Oxigraph.

**Deliberate deviations** (sanctioned here, detailed in the matrix): SPARQL-Update
PATCH only (no LD-PATCH), custom `X-FDP-Page-*` paging (not LDP Paging), and no
LDP Non-RDF Sources. **Remaining gaps** (all SHOULD/MAY, tracked in the matrix):
`Allow`/`Accept-Post` on 4xx error envelopes, `Prefer`-based container
minimisation, and an external W3C `ldp-testsuite` run in CI.
