# LDP conformance matrix

**Status:** v0.2 (task 15.1)
**Last updated:** 2026-06-12
**Spec:** W3C *Linked Data Platform 1.0* Recommendation — <https://www.w3.org/TR/ldp/>
**Authoritative ADR:** [ADR-0008](../adr/0008-full-ldp-with-patch.md)

This document records, requirement by requirement, where the FDP server conforms
to the W3C LDP Recommendation, where it **deliberately deviates** (with the
rationale and the ADR that sanctions it), and where it has a **gap** still to
close. It is the companion to the automated suite in
[`tests/conformance/test_ldp.py`](../../tests/conformance/test_ldp.py), which
encodes the load-bearing rows as HTTP-level assertions against a real triple
store; the "Test" column names the function that exercises each row.

> **On the requirement wording.** The LDP Recommendation states its normative
> requirements inline across §4–§5 rather than in a numbered checklist. The rows
> below **paraphrase** each requirement and cite the spec section; the MUST /
> SHOULD / MAY label reflects the spec's normative force for that statement. Read
> the cited section for the exact text. Status legend:
>
> | Symbol | Meaning |
> |---|---|
> | ✅ Conformant | Implemented and (where marked) covered by the conformance suite. |
> | ⚠️ Partial | Implemented for the common case; a sub-clause is unmet (see note). |
> | ↔️ Deviation | Intentionally divergent; sanctioned by an ADR. |
> | ❌ Gap | Not yet implemented; tracked as future work. |

## Conformance classes claimed

LDP defines layered conformance classes (§2). The FDP claims:

- **LDP Resource (LDPR)** — every record, schema, policy, license, and resource
  definition.
- **LDP RDF Source (LDP-RS)** — all of the above; the FDP has no LDP **Non-RDF**
  Sources. Binary distribution payloads are served by the data provider
  (`data/router.py`), *not* as LDP-NR resources, so the LDP-NR clauses (§4.2.x
  for non-RDF representations, `Link: rel=describedby`) are **out of scope by
  design**.
- **LDP Direct Container (LDP-DC)** — the FDP hierarchy
  (FAIRDataPoint → Catalog → Dataset → Distribution) is modelled as Direct
  Containers (§5.4). The FDP does **not** claim Basic or Indirect Container
  conformance: every FDP container carries explicit membership configuration and
  is therefore a Direct Container, never a bare Basic Container.

The FDP does **not** claim LDP **Paging** (§7) conformance — see
[Deviations](#deliberate-deviations).

## LDP Resource (LDPR / LDP-RS) — §4

| # | Requirement (paraphrased) | Force | Status | Notes | Test |
|---|---|---|---|---|---|
| R1 | Advertise interaction model via `Link: rel="type"` listing `ldp:Resource` (and `ldp:RDFSource` for an RDF source). | MUST | ✅ | `_response_headers` emits `ldp:Resource` + `ldp:RDFSource` on every response; containers add `ldp:Container` + `ldp:DirectContainer`. | `test_get_root_advertises_ldp_interaction_model` |
| R2 | Respond to `GET` with an RDF representation; **Turtle** must be supported. | MUST | ✅ | Content negotiation across Turtle / JSON-LD / RDF-XML / N-Triples. | `test_content_negotiation_turtle_and_jsonld_and_406` |
| R3 | Support additional serializations via content negotiation; honour `Accept`. | SHOULD | ✅ | `select_media_type`; JSON-LD verified. Unsupported `Accept` → **406**. | `test_content_negotiation_turtle_and_jsonld_and_406` |
| R4 | Support `HEAD`; same headers as `GET`, no body. | MUST | ✅ | `http_head` mirrors `http_get` header construction. | `test_head_matches_get_headers_without_body` |
| R5 | Support `OPTIONS`; respond with an `Allow` header listing supported methods. | MUST | ✅ | `http_options` → 204 with `Allow`, `Accept-Patch`, and (containers) `Accept-Post`. | `test_get_root_advertises_ldp_interaction_model` (Allow on GET) |
| R6 | Publish a strong validator (`ETag`) on `GET`/`HEAD` responses. | MUST | ✅ | `compute_etag` (BLAKE2b over canonical sorted N-Triples), quoted strong ETag. | `test_get_root_advertises_ldp_interaction_model`, `…_if_match_…` |
| R7 | Support conditional `PUT`/`DELETE`/`PATCH` via `If-Match`; reject a mismatch with **412**. | SHOULD | ✅ | `_enforce_if_match`: stale ETag → 412; `If-Match: *` on a missing resource → 412. | `test_put_create_then_if_match_concurrency_then_delete` |
| R8 | Require a precondition on an unconditional update of an existing resource. | SHOULD | ✅ | Missing `If-Match` on an existing resource → **428 Precondition Required** (RFC 6585) for `PUT`/`PATCH`/`DELETE`. | `test_put_create_then_if_match_concurrency_then_delete` |
| R9 | Support `PUT` to create or replace an LDPR; **201** on create with `Location`, **200** on replace. | MAY (create) | ✅ | `http_put`: create → 201 + `Location`; replace → 200. SHACL-validated against the resource's own type shape. | `test_put_create_then_if_match_concurrency_then_delete` |
| R10 | Support `PATCH`; advertise the accepted patch format via `Accept-Patch`. | MAY | ↔️ | `PATCH` accepts `application/sparql-update` only (no LD-PATCH); `Accept-Patch` advertised on all responses. See [Deviations](#deliberate-deviations). | `test_get_root_advertises_ldp_interaction_model` (Accept-Patch) |
| R11 | Reject a request body whose `Content-Type` is unsupported with **415**. | MUST | ✅ | `_parse_body` / PATCH content-type check → 415. | `test_put_rejects_unsupported_media_type` |
| R12 | A constrained resource advertises its constraints via `Link: rel="http://www.w3.org/ns/ldp#constrainedBy"`. | SHOULD | ✅ | `GET`/`HEAD` add `constrainedBy` pointing at the resource's SHACL shape when known. | `test_get_root_advertises_ldp_interaction_model` |
| R13 | A `405`/`415` error response carries the relevant advisory header (`Allow` / `Accept-Post` / `Accept-Patch`). | SHOULD (MUST for 405 `Allow`) | ✅ | `FDPError` carries optional advisory headers, emitted by both the handler and the catch-all middleware. A 405 (POST to a leaf) carries `Allow` (RFC 7231 §6.5.5); a 415 on a container POST carries `Accept-Post`; a 415 on PATCH carries `Accept-Patch`. | `test_post_to_leaf_is_method_not_allowed`; unit `test_post_unsupported_media_type_advertises_accept_post`, `test_patch_unsupported_media_type_advertises_accept_patch` |

## LDP Container (LDPC / LDP Direct Container) — §5

| # | Requirement (paraphrased) | Force | Status | Notes | Test |
|---|---|---|---|---|---|
| C1 | A container advertises `ldp:Container` (and its specific type, here `ldp:DirectContainer`) in `Link: rel="type"`, in addition to the LDPR types. | MUST | ✅ | Containers add both `ldp:Container` and `ldp:DirectContainer`. | `test_get_root_advertises_ldp_interaction_model`, `test_head_matches_get_headers_without_body` |
| C2 | A Direct Container declares `ldp:membershipResource` and exactly one of `ldp:hasMemberRelation`/`ldp:isMemberOfRelation`. | MUST | ✅ | `direct_container_config`: `membershipResource` = the container itself; **one `ldp:hasMemberRelation` per resource-definition child link** (a Catalog → `dcat:dataset` + `dcat:service`). | `test_created_catalog_is_a_direct_container` |
| C3 | A Direct Container declares `ldp:insertedContentRelation` (defaulting to `ldp:MemberSubject`). | MUST | ✅ | Always `ldp:insertedContentRelation ldp:MemberSubject`. | `test_created_catalog_is_a_direct_container` |
| C4 | On `POST`, create the member, mint its URI, and on success return **201** with a `Location` header. | MUST (if POST supported) | ✅ | `http_post`: mints the member IRI (honours `Slug`), 201 + `Location`. | (live on GraphDB; `test_post_to_leaf_is_method_not_allowed` covers the negative) |
| C5 | On member creation, add the membership triple per the container's membership pattern (and `ldp:contains`). | MUST | ✅ | `ContainmentManager` writes `ldp:contains` + the typed forward relation; membership is read directly from the container's Direct-Container config. | `test_created_catalog_is_a_direct_container` |
| C6 | `POST` to a resource that is **not** a container → **405**. | MUST | ✅ | `http_post` raises `MethodNotAllowed` when `registry.is_container` is false (e.g. a Distribution leaf). | `test_post_to_leaf_is_method_not_allowed` |
| C7 | `GET` on a container returns its containment/membership triples by default. | MUST | ✅ | The container's stored graph holds `ldp:contains` + membership triples; `GET` serializes them inline. | — |
| C8 | Honour the `Prefer` header (`return=representation` with `include`/`omit`) to minimise containment/membership triples. | SHOULD | ✅ | A container `GET` parses `Prefer`: `omit` of `ldp:PreferContainment`/`ldp:PreferMembership` (or `include` of `ldp:PreferMinimalContainer`) drops the `ldp:contains` and/or membership triples, the Direct-Container config is kept, and the response carries `Preference-Applied: return=representation`. Container responses also advertise `Vary: Prefer`. | `test_container_prefer_minimisation`; unit `test_get_container_prefer_omits_containment_and_membership`, `..._prefer_minimal_container_omits_both`, `..._advertises_vary_prefer` |
| C9 | Advertise the accepted POST media types via `Accept-Post` on the container. | MUST | ✅ | `Accept-Post` is emitted on containers only (not leaves). | `test_get_root_advertises_ldp_interaction_model` |
| C10 | `Allow` reflects the container method set (adds `POST`). | MUST | ✅ | Containers advertise `GET, HEAD, OPTIONS, POST, PUT, PATCH, DELETE`; leaves omit `POST`. | `test_get_root_advertises_ldp_interaction_model` |

## SHACL-on-write (FDP extension beyond LDP)

LDP delegates body validation to `ldp:constrainedBy`. The FDP enforces it: a
`POST` is validated against the container's member shape, a `PUT`/`PATCH` against
the resource's own type shape; a violation → **422** with the SHACL report. This
is stricter than LDP requires and is intentional (ADR-0008 "SHACL-on-write").

## Deliberate deviations

These are divergences the project has chosen, each sanctioned by an ADR. They are
**not** gaps.

| Deviation | LDP feature foregone | Rationale | ADR |
|---|---|---|---|
| **SPARQL-Update PATCH only** | LD-PATCH (`application/ldpatch`, §4.2.7 / LD-PATCH note) | SPARQL Update reuses the SPARQL endpoint's parser, classifier, and authorization pipeline, and is more widely supported by RDF client libraries. LD-PATCH may be added in v1.x as an additional accepted content type without architectural change. | [ADR-0008](../adr/0008-full-ldp-with-patch.md) |
| **Custom `X-FDP-Page-*` paging** | LDP Paging (§7) — `Prefer`-driven first-page links + `Link: rel="next"` page resources | The client's discovery pages need a simple offset/limit contract; LDP Paging's page-resource model is heavier than the FDP's needs. Documented and stable as `X-FDP-Page-*` headers on the `/page` extension. | [ADR-0008](../adr/0008-full-ldp-with-patch.md), architecture §10 |
| **No LDP Non-RDF Sources** | LDP-NR (§4.2 non-RDF representations, `Link: rel="describedby"`) | Binary payloads are served by the data provider via `dcat:downloadURL`; metadata records are pure RDF. There is no LDP-NR resource class in the FDP. | architecture §5.6 |
| **Property-level write scope** | (LDP imposes none; noted for completeness) | `odrl:modify` on a record permits `PATCH` of any property in it; sub-record access control is a v2 concern. | [ADR-0008](../adr/0008-full-ldp-with-patch.md) §"Property-level access control" |

## Gaps (future work)

| Gap | Requirement | Priority | Note |
|---|---|---|---|
| External W3C `ldp-testsuite` run | external validation (MAY) | Low | The in-repo suite (`tests/conformance/test_ldp.py`) now runs as a dedicated **`conformance` CI job** (`.github/workflows/ci.yml`) and gates the image build, so the LDP MUST/SHOULD matrix is checked on every PR. Running the *external* W3C Java `ldp-testsuite` against a dev instance and archiving the report would add independent third-party coverage; it needs a Java step and is optional. |

The two code-level SHOULD gaps from the previous revision — advisory headers on 4xx
(R13) and `Prefer` container minimisation (C8) — are now **implemented and tested**;
see those rows above.

## How the suite runs

[`tests/conformance/test_ldp.py`](../../tests/conformance/test_ldp.py) launches a
real Oxigraph + Postgres via testcontainers, auto-applies the default profile,
and authors as a steward/admin (with an **absolute** subject IRI — a relative one
produces N-Triples that strict, spec-conformant stores reject; see the fixture
note). All checks pass on Oxigraph, including the write/`If-Match`/`DELETE`
round-trip and the 405-on-POST-to-leaf check.

> Historical note: two write checks were once `xfail`ed on the belief that
> Oxigraph mishandled writes. The real cause was a test fixture using a relative
> subject IRI; Oxigraph was spec-correct. Fixed in task 15.1 — see the TASKS.md
> 15.1 note. This is unrelated to the genuine Oxigraph repeated-`named-graph-uri`
> multi-graph **read** under-projection (ADR-0004/0005, TASKS 10.3), which
> concerns the `/sparql` projection, not LDP writes.
