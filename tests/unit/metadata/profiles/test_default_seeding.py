"""Tests for the ADR-0012 profile-seeding additions (TASKS 14.5).

Covers the built-in default license set and the offer→managed-policy rewrite:
a seeded offer must end up as a self-consistent, profile-valid Offer at its
deployment-local IRI so the PDP resolver can fetch and parse it.
"""

from __future__ import annotations

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.metadata.profiles.applier import _rewrite_subject
from fdp.metadata.profiles.licenses import default_license_graphs
from fdp.policy.parser import parse_offer
from fdp.shared.namespaces import DCT, ODRL

BASE = "http://localhost:8000"

INTRINSIC = "https://w3id.org/fdp/profiles/default/offers/public-read-steward-modify"
OFFER_TTL = f"""\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
@prefix fdp-pol: <https://specs.fairdatapoint.org/odrl-profile#> .
<{INTRINSIC}> a odrl:Offer ;
    odrl:permission [ a odrl:Permission ; odrl:action odrl:read ] ;
    odrl:permission [
        a odrl:Permission ; odrl:action odrl:modify ;
        odrl:constraint [
            odrl:leftOperand fdp-pol:role ; odrl:operator odrl:eq ;
            odrl:rightOperand "steward"
        ]
    ] .
"""


@pytest.mark.unit
def test_default_license_set_is_seeded_at_local_iris() -> None:
    seeded = dict(default_license_graphs(BASE))
    assert set(seeded) == {
        f"{BASE}/fdp-api/licenses/cc0-1.0",
        f"{BASE}/fdp-api/licenses/cc-by-4.0",
        f"{BASE}/fdp-api/licenses/cc-by-sa-4.0",
    }
    g = seeded[f"{BASE}/fdp-api/licenses/cc-by-4.0"]
    subject = URIRef(f"{BASE}/fdp-api/licenses/cc-by-4.0")
    assert (subject, RDF.type, DCT.LicenseDocument) in g
    assert str(next(g.objects(subject, DCT.title))).startswith("Creative Commons")
    # Links to the canonical license IRI for cross-reference.
    assert URIRef("http://creativecommons.org/licenses/by/4.0/") in set(
        g.objects(subject, DCT.source)
    )


@pytest.mark.unit
def test_offer_rewrite_yields_parseable_managed_policy() -> None:
    source = Graph()
    source.parse(data=OFFER_TTL, format="turtle")
    managed = URIRef(f"{BASE}/fdp-api/policies/system-default")

    rewritten = _rewrite_subject(source, URIRef(INTRINSIC), managed)

    # The intrinsic subject is gone; the Offer now lives at the managed IRI.
    assert (URIRef(INTRINSIC), RDF.type, ODRL.Offer) not in rewritten
    assert (managed, RDF.type, ODRL.Offer) in rewritten
    # ...and the resolver's parser accepts it at the managed IRI.
    offer = parse_offer(rewritten, managed)
    assert offer.iri == str(managed)
    assert len(offer.permissions) == 2
    # Blank-node rules are preserved (same triple count).
    assert len(rewritten) == len(source)


@pytest.mark.unit
def test_rewrite_leaves_unrelated_nodes_untouched() -> None:
    g = Graph()
    a, b = URIRef("urn:a"), URIRef("urn:b")
    g.add((a, DCT.title, b))
    out = _rewrite_subject(g, URIRef("urn:x"), URIRef("urn:y"))
    assert (a, DCT.title, b) in out


@pytest.mark.unit
async def test_seeded_default_licenses_conform_to_the_license_shape() -> None:
    # The built-in license set must satisfy the shipped license SHACL shape, so a
    # re-validation (or client round-trip) of a seeded license passes.
    from fdp.metadata.licenses import (
        LICENSE_SHAPE_IRI,
        _probe_graph,
        predefined_license_shape_graph,
    )
    from fdp.metadata.shacl import InMemoryShapeProvider, ShaclValidator

    validator = ShaclValidator(
        InMemoryShapeProvider(
            {LICENSE_SHAPE_IRI: predefined_license_shape_graph().serialize(format="turtle")}
        )
    )
    for iri, graph in default_license_graphs(BASE):
        report = await validator.validate_against(_probe_graph(graph, iri), LICENSE_SHAPE_IRI)
        assert report.conforms, (iri, [v.message for v in report.violations])
