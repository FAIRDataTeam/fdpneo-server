# FAIR Signposting conformance

**Status:** v0.4.0 (task 17.4)
**Last updated:** 2026-07-03
**Profile:** *FAIR Signposting Profile* — <https://signposting.org/FAIR/>
**Specs:** RFC 8288 (Web Linking); DCAT 3; ADMS
**Authoritative ADR:** [ADR-0017](../adr/0017-alternative-identifiers-and-signposting.md) §2

The FDP server implements **FAIR Signposting Level 1**: typed RFC 8288 `Link`
relations on every successful `GET`/`HEAD` of an existing record, so a machine
agent can navigate and cite a record from its HTTP response headers alone,
without parsing the body. The link set is built by the pure
[`metadata/signposting.py`](../../src/fdp/metadata/signposting.py) and appended to
the response `Link` header by the LDP handlers (after the LDP `rel="type"` /
`ldp:constrainedBy` links). Unit + contract coverage:
[`tests/unit/metadata/test_signposting.py`](../../tests/unit/metadata/test_signposting.py),
[`tests/contract/test_signposting_headers.py`](../../tests/contract/test_signposting_headers.py).

## Relations emitted

| Relation | Source | Notes |
|---|---|---|
| `cite-as` | selection order below | Exactly one per response. |
| `describedby` | the canonical IRI, once per supported RDF media type | Carries a `type` attribute (`text/turtle`, `application/ld+json`, `application/rdf+xml`, `application/n-triples`). |
| `type` | each `rdf:type` of the canonical subject | e.g. `dcat:Dataset`. |
| `license` | IRI-valued `dct:license` | |
| `author` | IRI-valued `dct:creator` / `dct:publisher` | De-duplicated. |
| `item` | container members: `ldp:contains` + each typed member relation (`ldp:hasMemberRelation`) | Downward navigation; trimmed first under the cap. |
| `collection` | `dct:isPartOf` | Upward navigation. |

### `cite-as` selection (ADR-0017 §2)

The identifier a consumer should cite, in order — the first that applies wins,
ties broken lexicographically for determinism:

1. a client-asserted `owl:sameAs` whose object is under a recognised PID resolver;
2. an `adms:identifier` (`skos:notation`) or IRI-valued `dct:identifier` under a
   recognised PID resolver;
3. the record's **canonical** IRI (used even when the request arrived on a
   serving origin).

Recognised PID resolvers: `doi.org`, `dx.doi.org`, `hdl.handle.net`, `w3id.org`,
`purl.org`, `identifiers.org`, plus `ark:` scheme IRIs. This restores citation
primacy to a client-supplied PID even though the FDP serves the record at its own
canonical IRI.

## Cap and Level-2 deferral

To keep the header bounded, the total number of signposting links per response is
capped at `signposting.MAX_LINKS` (30). The fixed relations (`cite-as`,
`describedby`, `type`, `license`, `author`, `collection`) are always emitted;
surplus `item` links are trimmed first — acceptable Level-1 degradation for a
large container.

**Level 2 is deferred.** A `linkset` document (a separate resource holding the
full link set for cases too large for headers, `application/linkset+json`) is not
served in v0.4.0. When header size becomes a practical problem, a linkset endpoint
can be added behind the same selection logic (ADR-0017 §2). DOI/Handle *minting*
is out of scope — the FDP only signposts identifiers a client brought along.

## `HEAD`

`HEAD` returns the same `Link` header as `GET` (no body), so an agent can harvest
the signposting without fetching the representation.
