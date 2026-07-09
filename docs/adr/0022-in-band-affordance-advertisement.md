# ADR-0022: In-band affordance advertisement — closing the HATEOAS gap

**Status:** Proposed
**Date:** 2026-07-09
**Extends:** [ADR-0017](0017-alternative-identifiers-and-signposting.md) (Signposting §2); amends the LDP-paging deviation recorded in `docs/conformance/ldp-conformance.md`

## Context

A HATEOAS audit (2026-07-09) of both this server and the Java reference
implementation confirmed that the LDP surface is a strong Richardson
Level-3 citizen: interaction-model `Link` headers, `constrainedBy`,
`Allow`/`Accept-Post`/`Accept-Patch`, FAIR Signposting Level 1, and
bidirectionally maintained containment triples (`ldp:contains` + typed
DCAT relations downward, `dct:isPartOf` upward) mean a generic RDF/LDP
client can traverse and write the whole metadata tree from the root IRI
alone.

The audit also confirmed four places where a client still needs
out-of-band knowledge (URL-template conventions or the OpenAPI
document) instead of following links:

1. **Pagination is non-standard.** `GET /{urlPrefix}/page/{childPrefix}`
   (`metadata/extensions.py`) reports position via custom
   `X-FDP-Page-Total/Offset/Limit` headers. This was an explicitly
   sanctioned deviation ("no LDP Paging"), but it costs us the one part
   of paging that generic clients *do* understand: RFC 8288
   `Link: rel="next"` chains. The Java reference implementation emits
   `first/prev/next/last` links here; we regressed relative to it.

2. **Management affordances are invisible.** The `/spec` views,
   `/expanded`, `/page/{childPrefix}`, `POST …/state`, and the
   `<record>/meta` sibling graph are reachable only by suffix
   convention. A record's representation never mentions them.

3. **Publication state is not an affordance.** DRAFT/PUBLISHED/ARCHIVED
   lives in the `/meta` graph; nothing on the record response points a
   client at either the state or the transition endpoint.

4. **The JSON discovery catalog returns fragments, not links.**
   `GET /fdp-api/resource-definitions` (`metadata/rd_api.py`) exposes
   bare `urlPrefix` strings; the client concatenates them onto a base
   URL it must already know. Similarly, the OpenAPI document itself is
   not discoverable from the API root.

The fix must respect two existing commitments: the server is
**RDF-native** (no HAL/JSON:API envelope — the API *is* the RDF plus
HTTP link mechanics, ADR-0008/0017), and **record graphs carry only
client-authored + profile-mandated triples** (we do not want to inject
server-endpoint plumbing into the metadata a harvester will re-serve).

## Decision

Advertise every remaining affordance in-band, using the two channels
already established: RFC 8288 `Link` headers (extending the ADR-0017
signposting builder) and, where a *service* is being advertised, root
record triples (extending the ADR-0018 service advertisement).

### 1. Standard pagination links on `/page/{childPrefix}`

The paging endpoint additionally emits RFC 8288 navigation links
computed from `offset`/`limit`/`total`:

- `rel="first"` and `rel="last"` always;
- `rel="prev"` when `offset > 0`; `rel="next"` when
  `offset + limit < total`.

The `X-FDP-Page-*` headers remain for one minor release, documented as
deprecated, and are removed in the following release. The
`ldp-conformance.md` deviation entry is rewritten: we still do not
implement LDP Paging (the W3C Note's `Prefer`-driven protocol), but we
do speak standard Web Linking for page navigation.

### 2. Management-affordance link relations on record GET/HEAD

`metadata/signposting.py` grows a second pure builder,
`affordance_links(canonical_iri, *, is_container, url_prefix)`, appended
by the LDP router alongside the existing signposting links:

- `<{record}/meta>` with the extension relation type
  **`https://w3id.org/fdp/o#hasMetaMetadata`** — the provenance/state
  sibling graph. (RFC 8288 §2.1.2 admits absolute-IRI extension
  relations; we deliberately do not overload `describedby`, which
  ADR-0017 reserves for alternate serializations of the record itself.)
- `<{record}/spec>` (instance) and `<{base}/{urlPrefix}/spec>` (type
  level, the one create-forms need) with
  **`https://w3id.org/fdp/o#hasSpec`**. `ldp:constrainedBy` continues to
  carry the shape's *storage IRI*; these rels advertise the negotiated
  *views*.
- `<{record}/expanded>` with **`https://w3id.org/fdp/o#hasExpandedView`**.
- `<{record}/state>` with **`https://w3id.org/fdp/o#hasStateTransition`**
  — emitted only for callers whose context could ever transition state
  is *not* attempted (authorization is per-request, PDP-gated); the link
  is advertised unconditionally, and the endpoint answers 401/403 as
  today. Hypermedia advertises the affordance; policy decides.
- For containers, `<{container}/page/{childPrefix}>` per child link with
  **`https://w3id.org/fdp/o#hasMemberPage`**.

The exact FDP-O terms above are proposals under our own namespace; if
the FDP-O working group standardizes equivalents, a later ADR swaps the
IRIs (extension rels are opaque strings to conforming clients, so this
is a compatible substitution). The `MAX_LINKS` cap and its
trim-`item`-first policy apply to the combined link set; affordance
links count as fixed relations (never trimmed).

### 3. Publication state surfaced in the `/meta` representation and root

No change to the record graph (per the RDF-purity commitment). The
state is already a triple in the meta graph; task 2's `hasMetaMetadata`
link makes it *discoverable*. The meta graph additionally gains the
allowed next transitions as triples
(`<record> fdp-o:allowedStateTransition "PUBLISHED" …`), computed from
the lifecycle state machine at read time, so a client learns both where
the state lives and what it may become, without consulting OpenAPI.

### 4. Self-describing JSON catalog and discoverable API description

- `ResourceDefinitionView` gains a `links` object with **absolute**
  URLs: `container` (`{base}/{urlPrefix}`), `spec`
  (`{base}/{urlPrefix}/spec`), and `self`
  (`{base}/fdp-api/resource-definitions/{slug}`). `urlPrefix` stays for
  compatibility.
- The API root (`GET {base}/`, the FDP record) response gains
  `Link: </fdp-api/openapi.json>; rel="service-desc"` and
  `Link: </fdp-api/docs>; rel="service-doc"` (both IANA-registered
  relations), plus an extension link to `/fdp-api/resource-definitions`
  (**`https://w3id.org/fdp/o#hasResourceDefinitions`**). The ADR-0018
  triple-level service advertisement (`void:sparqlEndpoint`,
  `dcat:DataService`) is unchanged.

## Alternatives considered

- **HAL / Hydra / JSON:API envelope** — rejected. The machine surface
  is RDF + Web Linking; adding a second hypermedia format doubles the
  contract without adding capability. Hydra specifically would express
  operations elegantly but has negligible client uptake in the FDP
  ecosystem, and the MCP sidecar (ADR-0018) already covers the
  agent-consumption case.
- **Affordance triples inside the record graph** (e.g.
  `<record> fdp-o:hasSpec <…/spec>` as stored metadata) — rejected:
  pollutes harvested metadata with deployment plumbing, survives export
  (`fdp dump`) where it is meaningless, and violates the
  client-authored-graph discipline of ADR-0016/0017. Headers are
  per-response and cost nothing at rest.
- **Overloading registered relations** (`describedby` for `/meta`,
  `profile` for `/spec`) — rejected: `describedby` is already bound to
  alternate serializations (ADR-0017) and RFC 6906 `profile` denotes a
  constraint document *for the representation being served*, not a
  separate fetchable view. Precise extension IRIs beat approximate
  registered ones.
- **Full LDP Paging (W3C Note)** — rejected again, same grounds as the
  original deviation: the Note is not a REC, client support is absent,
  and `Prefer`-driven paging complicates caching. RFC 8288 `next`/`prev`
  gives generic clients everything they actually use.
- **Signposting Level 2 `linkset`** — still deferred. The affordance
  links add ≤ 6 fixed relations per response; the `MAX_LINKS` analysis
  from ADR-0017 still holds. Revisit when a real deployment hits the
  cap.

## Consequences

- A generic client can now go from any record to its schema views,
  expanded view, provenance/state, paging, and the API description
  without any URL construction — the last out-of-band dependencies
  become optimizations, not requirements.
- **Header growth** on record responses (~4–6 links). Bounded by
  `MAX_LINKS`; CORS already exposes `Link`.
- **New vocabulary surface:** five proposed FDP-O extension terms.
  They live only in headers (and `allowedStateTransition` in meta
  graphs), so a later IRI substitution is cheap.
- **Deprecation contract:** `X-FDP-Page-*` removed one minor release
  after the standard links ship; `fdp-client` must migrate its paging
  code and regenerate types for `ResourceDefinitionView.links`.
- The conformance doc's deviation list shrinks by one and gains one
  clarification (Web Linking pagination ≠ LDP Paging).
- Code touched: `metadata/signposting.py`, `metadata/ldp/router.py`
  (`_response_headers`), `metadata/extensions.py` (paging links),
  `metadata/lifecycle.py` + `metadata/meta.py` (allowed transitions),
  `metadata/rd_api.py` (`links`), `main.py` (root service-desc),
  `docs/conformance/ldp-conformance.md`, tests throughout.

## References

- Audit: HATEOAS gap analysis of FDPneo vs. the Java reference
  implementation, 2026-07-09 (conversation record; summary mirrored in
  Phase 22 of `TASKS.md`).
- RFC 8288 (Web Linking), §2.1.2 (extension relation types); IANA Link
  Relations registry (`first`, `prev`, `next`, `last`, `service-desc`,
  `service-doc`).
- FAIR Signposting Profile (signposting.org/FAIR/); Richardson
  Maturity Model, level 3.
- Extends [ADR-0017](0017-alternative-identifiers-and-signposting.md);
  complements [ADR-0018](0018-agent-consumption-mcp-server.md) §G-05;
  respects [ADR-0008](0008-full-ldp-with-patch.md),
  [ADR-0010](0010-metadata-publication-state.md),
  [ADR-0016](0016-backup-restore-migration.md).
