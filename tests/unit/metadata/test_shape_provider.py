"""Unit tests for :class:`MetadataShapeProvider`."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from rdflib import Graph

from fdp.metadata.shacl import (
    ShaclValidator,
    UnknownShapeError,
    ValidationReport,
)
from fdp.metadata.shape_provider import MetadataShapeProvider, PredefinedShapeProvider

# A trivial SHACL shape that requires dct:title on dcat:Catalog records.
CATALOG_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
    sh:property [
        sh:path dct:title ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""


@dataclass
class _FakeRepo:
    """In-memory repo keyed by IRI → Turtle source."""

    graphs: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def get_graph(self, record_uri: str) -> Graph:
        self.calls.append(record_uri)
        g = Graph()
        ttl = self.graphs.get(record_uri)
        if ttl:
            g.parse(data=ttl, format="turtle")
        return g


# --- happy path ----------------------------------------------------------


@pytest.mark.unit
async def test_fetch_returns_turtle_for_a_known_shape() -> None:
    iri = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={iri: CATALOG_SHAPE_TTL})
    provider = MetadataShapeProvider(repo)  # type: ignore[arg-type]
    ttl = await provider.fetch(iri)
    assert "sh:NodeShape" in ttl
    assert "dcat:Catalog" in ttl
    assert repo.calls == [iri]


# --- unknown shape -------------------------------------------------------


@pytest.mark.unit
async def test_fetch_raises_unknown_for_empty_graph() -> None:
    repo = _FakeRepo()  # no shapes registered
    provider = MetadataShapeProvider(repo)  # type: ignore[arg-type]
    with pytest.raises(UnknownShapeError):
        await provider.fetch("http://example.org/missing-shape")


# --- integration with ShaclValidator -------------------------------------


@pytest.mark.unit
async def test_validator_uses_provider_for_validation_decisions() -> None:
    iri = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={iri: CATALOG_SHAPE_TTL})
    validator = ShaclValidator(MetadataShapeProvider(repo))  # type: ignore[arg-type]

    # A catalog with a title conforms.
    ok_graph = Graph()
    ok_graph.parse(
        data="""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://example.org/cat-1>
    a dcat:Catalog ;
    dct:title "Public catalog" .
""",
        format="turtle",
    )
    report: ValidationReport = await validator.validate_against(ok_graph, iri)
    assert report.conforms is True

    # A catalog without a title fails.
    bad_graph = Graph()
    bad_graph.parse(
        data="""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
<http://example.org/cat-2>
    a dcat:Catalog .
""",
        format="turtle",
    )
    report = await validator.validate_against(bad_graph, iri)
    assert report.conforms is False
    assert len(report.violations) >= 1


@pytest.mark.unit
async def test_predefined_provider_serves_from_code_without_delegating() -> None:
    iri = "https://w3id.org/fdp/o#LicenseDocumentShape"
    repo = _FakeRepo()  # store has nothing — simulates an already-applied deployment
    provider = PredefinedShapeProvider(
        predefined={iri: CATALOG_SHAPE_TTL},
        delegate=MetadataShapeProvider(repo),  # type: ignore[arg-type]
    )
    ttl = await provider.fetch(iri)
    assert "sh:NodeShape" in ttl
    assert repo.calls == [], "predefined shape must not hit the triple store"


@pytest.mark.unit
async def test_predefined_provider_delegates_unknown_iris() -> None:
    known = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={known: CATALOG_SHAPE_TTL})
    provider = PredefinedShapeProvider(
        predefined={"urn:served-from-code": CATALOG_SHAPE_TTL},
        delegate=MetadataShapeProvider(repo),  # type: ignore[arg-type]
    )
    assert "dcat:Catalog" in await provider.fetch(known)
    assert repo.calls == [known]
    with pytest.raises(UnknownShapeError):
        await provider.fetch("http://example.org/missing")


@pytest.mark.unit
async def test_license_shape_resolves_even_when_unseeded() -> None:
    # Regression (client report): PUT/validate /licenses 500'd with
    # UnknownShapeError when the license shape was not seeded in the store. The
    # composite provider resolves the server-owned shape from code instead.
    from fdp.metadata.licenses import LICENSE_SHAPE_IRI, predefined_license_shape_graph

    repo = _FakeRepo()  # store is empty — shape was never seeded
    validator = ShaclValidator(
        PredefinedShapeProvider(
            predefined={
                LICENSE_SHAPE_IRI: predefined_license_shape_graph().serialize(format="turtle")
            },
            delegate=MetadataShapeProvider(repo),  # type: ignore[arg-type]
        )
    )
    licence = Graph()
    licence.parse(
        data=(
            "@prefix dct: <http://purl.org/dc/terms/> ."
            "@prefix fdp: <https://w3id.org/fdp/o#> ."
            ' <urn:l> a fdp:ManagedLicense ; dct:title "CC BY 4.0" .'
        ),
        format="turtle",
    )
    report = await validator.validate_against(licence, LICENSE_SHAPE_IRI)
    assert report.conforms is True


@pytest.mark.unit
async def test_validator_bootstrap_warms_the_cache() -> None:
    iri = "http://www.w3.org/ns/dcat#Catalog"
    repo = _FakeRepo(graphs={iri: CATALOG_SHAPE_TTL})
    validator = ShaclValidator(MetadataShapeProvider(repo))  # type: ignore[arg-type]

    assert iri not in validator.cached_shapes()
    await validator.bootstrap([iri])
    assert iri in validator.cached_shapes()
    # The provider was called once during bootstrap.
    assert repo.calls == [iri]

    # A subsequent validate hits the cache — no extra fetch.
    graph = Graph()
    graph.parse(
        data="""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://example.org/c>
    a dcat:Catalog ;
    dct:title "T" .
""",
        format="turtle",
    )
    await validator.validate_against(graph, iri)
    assert repo.calls == [iri]  # still just the bootstrap call
