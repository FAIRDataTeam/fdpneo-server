"""Tests for ``fdpneo_server.metadata.identifiers`` — dual identifier model (ADR-0014/0017)."""

from __future__ import annotations

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdpneo_server.metadata.identifiers import reconcile_identifiers
from fdpneo_server.shared.errors import AmbiguousSubject
from fdpneo_server.shared.namespaces import ADMS, DCAT, DCT, OWL, SKOS, XSD

ID_BASE = "https://w3id.org/myfdp"
CANON = f"{ID_BASE}/catalog/c1"


class TestReconcile:
    def test_relative_subject_is_noop(self) -> None:
        # Body authored with <> → parsed to the canonical IRI; nothing to rebind.
        g = Graph()
        canon = URIRef(CANON)
        g.add((canon, RDF.type, DCAT.Catalog))
        g.add((canon, DCT.title, Literal("My catalog")))
        out = reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)
        assert out is g  # unchanged, same object
        assert (canon, DCT.title, Literal("My catalog")) in out

    def test_foreign_primary_subject_rebound_as_alternative_identifier(self) -> None:
        # ADR-0017 §1: a foreign subject is rebound and preserved as structured
        # alternative identifiers (dct:identifier + adms:identifier), never sameAs.
        foreign = URIRef("https://doi.org/10.1234/foo")
        g = Graph()
        g.add((foreign, RDF.type, DCAT.Catalog))
        g.add((foreign, DCT.title, Literal("Brought-along ID")))
        out = reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)
        canon = URIRef(CANON)
        # Triples rebound to canonical; the foreign IRI is no longer a subject.
        assert (canon, DCT.title, Literal("Brought-along ID")) in out
        assert (canon, RDF.type, DCAT.Catalog) in out
        assert (foreign, RDF.type, DCAT.Catalog) not in out
        # The server never mints owl:sameAs.
        assert not list(out.triples((None, OWL.sameAs, None)))
        # dct:identifier literal (DCAT 3 lightweight form).
        assert (canon, DCT.identifier, Literal(str(foreign))) in out
        # adms:identifier node: typed adms:Identifier with skos:notation ^^xsd:anyURI.
        node = out.value(canon, ADMS.identifier)
        assert node is not None
        assert (node, RDF.type, ADMS.Identifier) in out
        assert (node, SKOS.notation, Literal(str(foreign), datatype=XSD.anyURI)) in out

    def test_self_reference_object_is_rebound(self) -> None:
        foreign = URIRef("https://example.org/thing")
        child = URIRef("https://example.org/thing/part")
        g = Graph()
        g.add((foreign, RDF.type, DCAT.Catalog))
        g.add((foreign, DCAT.dataset, child))
        g.add((child, DCAT.distribution, foreign))  # back-reference to primary
        out = reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)
        canon = URIRef(CANON)
        assert (child, DCAT.distribution, canon) in out
        assert (canon, DCAT.dataset, child) in out

    def test_within_base_mismatch_rebound_without_sameas(self) -> None:
        # A subject under our base but != canonical is a mis-addressing: correct
        # it to canonical, but do NOT assert sameAs between two of our own IRIs.
        other = URIRef(f"{ID_BASE}/catalog/typo")
        g = Graph()
        g.add((other, RDF.type, DCAT.Catalog))
        g.add((other, DCT.title, Literal("x")))
        out = reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)
        canon = URIRef(CANON)
        assert (canon, DCT.title, Literal("x")) in out
        # A within-base mis-addressing is silently corrected: no cross-reference
        # of any kind (sameAs, dct:identifier, adms:identifier) is fabricated.
        assert not list(out.triples((None, OWL.sameAs, None)))
        assert not list(out.triples((None, ADMS.identifier, None)))
        assert not list(out.triples((canon, DCT.identifier, None)))

    def test_explicit_external_ids_preserved(self) -> None:
        canon = URIRef(CANON)
        g = Graph()
        g.add((canon, RDF.type, DCAT.Catalog))
        g.add((canon, DCT.identifier, Literal("ACME-2024-001")))
        g.add((canon, SKOS.exactMatch, URIRef("https://doi.org/10.1234/foo")))
        out = reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)
        assert (canon, DCT.identifier, Literal("ACME-2024-001")) in out
        assert (canon, SKOS.exactMatch, URIRef("https://doi.org/10.1234/foo")) in out

    def test_ambiguous_multiple_typed_subjects_raises(self) -> None:
        # ADR-0016 §1: no "store as authored" fallback — a body with several
        # typed IRI subjects has no unambiguous primary subject → 400.
        a = URIRef("https://example.org/a")
        b = URIRef("https://example.org/b")
        g = Graph()
        g.add((a, RDF.type, DCAT.Catalog))
        g.add((b, RDF.type, DCAT.Dataset))
        with pytest.raises(AmbiguousSubject):
            reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)

    def test_zero_typed_subjects_raises(self) -> None:
        # A foreign subject without any rdf:type is not a valid primary subject.
        g = Graph()
        g.add((URIRef("https://example.org/a"), DCT.title, Literal("no type")))
        with pytest.raises(AmbiguousSubject):
            reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)

    def test_blank_node_only_body_raises(self) -> None:
        # A blank-node-only body cannot be keyed under the canonical IRI.
        g = Graph()
        b = BNode()
        g.add((b, RDF.type, DCAT.Catalog))
        g.add((b, DCT.title, Literal("anon")))
        with pytest.raises(AmbiguousSubject):
            reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)

    def test_canonical_trailing_slash_variant_is_noop(self) -> None:
        canon_slash = URIRef(CANON + "/")
        g = Graph()
        g.add((canon_slash, RDF.type, DCAT.Catalog))
        out = reconcile_identifiers(g, canonical_iri=CANON, identifier_base=ID_BASE)
        assert out is g
