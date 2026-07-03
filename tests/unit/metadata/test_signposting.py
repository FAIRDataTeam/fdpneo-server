"""Unit tests for ``fdp.metadata.signposting`` (ADR-0017 §2, FAIR Signposting L1)."""

from __future__ import annotations

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.signposting import (
    MAX_LINKS,
    Link,
    is_pid_iri,
    render_link_header,
    select_cite_as,
    signposting_links,
)
from fdp.shared.namespaces import ADMS, DCAT, DCT, LDP, OWL, SKOS, XSD

BASE = "https://w3id.org/myfdp"
CANON = f"{BASE}/catalog/c1"
DOI = "https://doi.org/10.1234/foo"
HANDLE = "https://hdl.handle.net/20.500/xyz"
ARK = "ark:/12345/abc"
TTL = "text/turtle"
JSONLD = "application/ld+json"


def _adms(g: Graph, subject: URIRef, notation: str) -> None:
    node = BNode()
    g.add((subject, ADMS.identifier, node))
    g.add((node, RDF.type, ADMS.Identifier))
    g.add((node, SKOS.notation, Literal(notation, datatype=XSD.anyURI)))


# --- PID recognition -------------------------------------------------------


@pytest.mark.unit
def test_is_pid_iri_recognises_resolvers_and_ark() -> None:
    assert is_pid_iri(DOI)
    assert is_pid_iri(HANDLE)
    assert is_pid_iri("https://w3id.org/x")
    assert is_pid_iri("https://purl.org/x")
    assert is_pid_iri("https://identifiers.org/x")
    assert is_pid_iri(ARK)  # ark: scheme
    # A port/userinfo doesn't fool the host check.
    assert is_pid_iri("https://doi.org:443/10.1/x")
    # Not a PID: an arbitrary host, or the FDP's own non-resolver base.
    assert not is_pid_iri("https://example.org/x")
    assert not is_pid_iri("https://fdp.example.org/catalog/c1")


# --- cite-as selection order -----------------------------------------------


@pytest.mark.unit
def test_cite_as_prefers_client_sameas_under_pid() -> None:
    g = Graph()
    canon = URIRef(CANON)
    g.add((canon, OWL.sameAs, URIRef(DOI)))  # tier 1
    _adms(g, canon, HANDLE)  # tier 2 also present
    assert select_cite_as(g, CANON) == DOI


@pytest.mark.unit
def test_cite_as_uses_adms_identifier_when_no_sameas() -> None:
    g = Graph()
    _adms(g, URIRef(CANON), DOI)
    assert select_cite_as(g, CANON) == DOI


@pytest.mark.unit
def test_cite_as_uses_iri_valued_dct_identifier_under_pid() -> None:
    g = Graph()
    g.add((URIRef(CANON), DCT.identifier, Literal(HANDLE)))
    assert select_cite_as(g, CANON) == HANDLE


@pytest.mark.unit
def test_cite_as_ignores_non_pid_cross_references() -> None:
    g = Graph()
    canon = URIRef(CANON)
    g.add((canon, DCT.identifier, Literal("ACME-2024-001")))  # not a PID
    g.add((canon, OWL.sameAs, URIRef("https://example.org/mirror")))  # not a resolver
    assert select_cite_as(g, CANON) == CANON


@pytest.mark.unit
def test_cite_as_tie_break_is_lexicographic() -> None:
    g = Graph()
    canon = URIRef(CANON)
    g.add((canon, OWL.sameAs, URIRef("https://doi.org/10.1/b")))
    g.add((canon, OWL.sameAs, URIRef("https://doi.org/10.1/a")))
    assert select_cite_as(g, CANON) == "https://doi.org/10.1/a"


@pytest.mark.unit
def test_cite_as_defaults_to_canonical() -> None:
    g = Graph()
    g.add((URIRef(CANON), DCT.title, Literal("no identifiers")))
    assert select_cite_as(g, CANON) == CANON
    # Trailing-slash canonical normalizes to the stored (slash-stripped) subject.
    assert select_cite_as(g, CANON + "/") == CANON


# --- link building ---------------------------------------------------------


@pytest.mark.unit
def test_links_cover_the_expected_relations() -> None:
    g = Graph()
    canon = URIRef(CANON)
    g.add((canon, RDF.type, DCAT.Catalog))
    g.add((canon, DCT.license, URIRef("https://creativecommons.org/licenses/by/4.0/")))
    g.add((canon, DCT.creator, URIRef("https://ror.org/006hf6230")))
    g.add((canon, DCT.publisher, URIRef("https://ror.org/006hf6230")))  # dedup with creator
    g.add((canon, DCT.isPartOf, URIRef(BASE)))

    links = signposting_links(g, CANON, (TTL, JSONLD))
    by_rel: dict[str, list[Link]] = {}
    for link in links:
        by_rel.setdefault(link.rel, []).append(link)

    assert [link.target for link in by_rel["cite-as"]] == [CANON]
    # describedby: canonical IRI once per media type, carrying the type attribute.
    assert {(link.target, link.type) for link in by_rel["describedby"]} == {
        (CANON, TTL),
        (CANON, JSONLD),
    }
    assert [link.target for link in by_rel["type"]] == [str(DCAT.Catalog)]
    assert by_rel["license"][0].target == "https://creativecommons.org/licenses/by/4.0/"
    # creator + publisher dedup to a single author link.
    assert [link.target for link in by_rel["author"]] == ["https://ror.org/006hf6230"]
    assert by_rel["collection"][0].target == BASE


@pytest.mark.unit
def test_item_links_from_contains_and_member_relations() -> None:
    g = Graph()
    canon = URIRef(CANON)
    child_a = f"{BASE}/dataset/a"
    child_b = f"{BASE}/dataset/b"
    g.add((canon, LDP.contains, URIRef(child_a)))
    # A typed member relation declared by the Direct Container config.
    g.add((canon, LDP.hasMemberRelation, DCAT.dataset))
    g.add((canon, DCAT.dataset, URIRef(child_b)))
    # Config triples must NOT become items.
    g.add((canon, LDP.membershipResource, canon))
    g.add((canon, LDP.insertedContentRelation, LDP.MemberSubject))

    items = {link.target for link in signposting_links(g, CANON, (TTL,)) if link.rel == "item"}
    assert items == {child_a, child_b}


@pytest.mark.unit
def test_links_are_capped_and_items_trimmed_first() -> None:
    g = Graph()
    canon = URIRef(CANON)
    g.add((canon, RDF.type, DCAT.Catalog))
    for i in range(100):
        g.add((canon, LDP.contains, URIRef(f"{BASE}/dataset/d{i:03d}")))

    links = signposting_links(g, CANON, (TTL, JSONLD))
    assert len(links) == MAX_LINKS
    # Fixed relations survive; items fill the remainder.
    assert any(link.rel == "cite-as" for link in links)
    assert sum(1 for link in links if link.rel == "describedby") == 2
    assert any(link.rel == "type" for link in links)
    n_items = sum(1 for link in links if link.rel == "item")
    assert n_items == MAX_LINKS - 4  # cite-as(1) + describedby(2) + type(1)


# --- RFC 8288 rendering ----------------------------------------------------


@pytest.mark.unit
def test_render_link_header_rfc8288() -> None:
    rendered = render_link_header(
        [
            Link(DOI, "cite-as"),
            Link(CANON, "describedby", type=TTL),
        ]
    )
    assert rendered == (f'<{DOI}>; rel="cite-as", <{CANON}>; rel="describedby"; type="text/turtle"')


@pytest.mark.unit
def test_render_empty_is_empty_string() -> None:
    assert render_link_header([]) == ""
