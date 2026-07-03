# ADR-0017: Structured alternative identifiers and FAIR Signposting

**Status:** Proposed
**Date:** 2026-07-03
**Amends:** [ADR-0014](0014-persistent-identifiers.md) (dual identifier model, §3)

## Context

ADR-0014's dual identifier model rebinds a foreign primary subject (a DOI, an
ARK, another organisation's IRI) to the canonical minted IRI and automatically
records the original as `owl:sameAs`. Review surfaced two problems with that
default, and one gap the model leaves open at the HTTP level.

**`owl:sameAs` is semantically too strong for what the server knows.** The
server observes only that *the client addressed this record by that IRI*.
`owl:sameAs` asserts full identity — every property of either resource holds
of the other, in both directions, for any OWL-aware consumer. If the foreign
IRI denotes the described dataset while the minted IRI accumulates
record-level or container-level statements, the automatic `sameAs` licenses
false inferences: the well-documented "sameAs is not the same" problem. The
assertion also degrades with multiplicity — a resource carrying a DOI, a
Handle, and an institutional IRI becomes a `sameAs` clique rather than a set
of typed identifiers.

**DCAT 3 already prescribes the right vocabulary.** Alternative and legacy
identifiers belong in `dct:identifier` (literal) and `adms:identifier`
(a typed `adms:Identifier` node carrying the notation and, optionally, the
scheme agency). That expresses exactly what the server knows — "this is also
an identifier of this resource" — without the identity commitment.

**No HTTP-level answer to "which URI do I cite?".** A record now legitimately
carries several URIs: the canonical minted IRI, possibly a client-supplied
PID, plus serving-origin URLs. The community convention for resolving this
for machine agents is **FAIR Signposting**: typed `Link` relations
(`cite-as`, `describedby`, `type`, `license`, `author`, `item`,
`collection`) on every response. The FDP emits `Link` headers already (LDP
types, `constrainedBy`), so the mechanism is in place.

## Decision

### 1. Demote the automatic `owl:sameAs` to structured alternative identifiers

When `reconcile_identifiers` rebinds a foreign primary subject to the
canonical IRI, it records the original as:

- `<canonical> dct:identifier "<foreign-iri>"` (literal), and
- `<canonical> adms:identifier [ a adms:Identifier ; skos:notation
  "<foreign-iri>"^^xsd:anyURI ]`.

`owl:sameAs` is **never asserted by the server**. Client-authored
cross-references (`owl:sameAs`, `skos:exactMatch`, `dct:identifier`,
`adms:identifier` attached to `<>`) continue to pass through untouched — a
client that truly means identity says so itself, and owns the claim.

Existing records carry `sameAs` triples added by the v0.3.0 behaviour; these
are **not** migrated, because server-added and client-authored `sameAs`
triples are indistinguishable after the fact. The new rule applies from this
version forward; the release notes flag the semantics change.

### 2. Adopt FAIR Signposting (Level 1) on record responses

Every LDP `GET`/`HEAD` response for an existing record adds signposting
`Link` relations derived from the record and meta graphs:

- **`cite-as`** — the identifier a consumer should cite. Selection order:
  1. a client-asserted `owl:sameAs` whose object is under a recognised PID
     resolver (`doi.org`, `hdl.handle.net`, `ark:`, `w3id.org`, `purl.org`,
     `identifiers.org`);
  2. an `adms:identifier` / IRI-valued `dct:identifier` under a recognised
     PID resolver;
  3. the canonical IRI itself.
  This is what restores first-class citation status to a client-supplied
  PID even though the FDP serves the record at its own canonical IRI.
- **`describedby`** — the record's alternate RDF serializations (same IRI,
  `type` attribute per supported media type), so a landing-page consumer
  finds the machine-readable form.
- **`type`** — the record's `rdf:type` IRIs (e.g. `dcat:Dataset`).
- **`license`** — the record's `dct:license`, when present.
- **`author`** — IRI-valued `dct:creator` / `dct:publisher`, when present.
- **`item` / `collection`** — derived from the containment relations the
  Direct Container config already stamps (`ldp:contains` and the typed
  member relations downward; `dct:isPartOf` upward).

Level 2 (a `linkset` document for link sets too large for headers) is
deferred until header size becomes a practical problem; the builder caps
per-response links and can grow a linkset endpoint behind the same
selection logic.

## Alternatives considered

- **Keep automatic `owl:sameAs`** — rejected: the server asserts a claim it
  cannot know, with reasoning consequences borne by every downstream
  consumer.
- **`skos:exactMatch` as the automatic link** — gentler than `sameAs` but
  still a mapping claim between *concepts*, and SKOS-domain-restricted;
  `adms:identifier` states precisely what is known.
- **Only `dct:identifier` literals** — loses the structured form consumers
  use to distinguish identifier schemes; DCAT 3 recommends carrying both.
- **Signposting in the HTML client only** — rejected: the API is the
  machine interface; agents harvesting the FDP never see the client's
  `<link>` elements.

## Consequences

- **Safer default semantics.** The server's automatic claims are now
  provable from what it observed; identity claims are exclusively
  client-authored.
- **Multiple identifiers become first-class** via repeatable
  `adms:identifier`, aligning with DCAT 3.
- **Machine-actionable citation.** Agents get an unambiguous `cite-as` on
  every record — client PIDs keep citation primacy where they exist.
- **Contract change for consumers of the old behaviour.** Anything that
  relied on server-minted `sameAs` (e.g. SPARQL queries joining on it) must
  switch to `adms:identifier`/`dct:identifier`.
- **New vocabulary surface.** `ADMS` joins the namespace registry; the
  resource SHACL shape gains optional `adms:identifier` (additive, lenient,
  consistent with the v0.2 posture).
- ADR-0016's import path inherits the same rule: bulk-imported foreign IRIs
  are recorded as alternative identifiers, `sameAs` only on explicit
  operator assertion.

## References

- FAIR F1/A1; DCAT 3 (dcat/#Property:resource_identifier,
  `adms:identifier`); ADMS (W3C NOTE); FAIR Signposting Profile
  (signposting.org/FAIR/); RFC 8288 (Web Linking).
- Amends [ADR-0014](0014-persistent-identifiers.md); refines
  [ADR-0016](0016-backup-restore-migration.md) §4.
- Code touched: `shared/namespaces.py` (ADMS), `metadata/identifiers.py`
  (`reconcile_identifiers`), new `metadata/signposting.py`,
  `metadata/ldp/router.py` (`_response_headers` extension), default
  resource shape, tests.
