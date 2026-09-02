"""Unit tests for the one-time FDP vocabulary migration (ADR-0026).

The fake triple-store adapter is a real rdflib ``Dataset`` so the SPARQL the
migration emits (the STRSTARTS enumeration, CONSTRUCT per graph) is actually
executed, not regex-faked — same discipline as ``test_lifecycle.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from rdflib import RDF, Dataset, Literal, URIRef

from fdpneo_server.metadata.licenses import LICENSE_SHAPE_IRI
from fdpneo_server.metadata.meta import META_SHAPE_IRI
from fdpneo_server.metadata.profiles.rd_records import RD_SHAPE_IRI
from fdpneo_server.metadata.vocab_migration import map_iri, migrate_vocabulary
from fdpneo_server.shared.namespaces import (
    FDP_FAIRDATAPOINT,
    FDP_METADATA_SERVICE,
    FDP_METADATA_STATE,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

OLD = "https://w3id.org/fdp/o#"
NEW = "https://w3id.org/fdp/fdp-o#"
BASE = "http://localhost:8000"


class _DatasetAdapter:
    """Triple-store adapter backed by a real rdflib ``Dataset``."""

    def __init__(self) -> None:
        self.ds = Dataset()

    async def query(self, sparql: str, *, accept: str = "", **_: Any) -> bytes:
        res = self.ds.query(sparql)
        fmt = "turtle" if res.type == "CONSTRUCT" else "json"
        out = res.serialize(format=fmt)
        return out if isinstance(out, bytes) else (out or "").encode("utf-8")

    async def replace_graph(
        self, graph_uri: str, body: bytes | str, *, mime: str = "application/n-triples"
    ) -> None:
        ctx = self.ds.get_context(URIRef(graph_uri))
        for triple in list(ctx):
            ctx.remove(triple)
        data = body.decode("utf-8") if isinstance(body, bytes) else body
        ctx.parse(data=data, format="nt" if "n-triples" in mime else "turtle")

    async def drop_graph(self, graph_uri: str) -> None:
        ctx = self.ds.get_context(URIRef(graph_uri))
        for triple in list(ctx):
            ctx.remove(triple)

    # test helpers
    def add(self, graph_uri: str, s: str, p: str, o: str | Literal) -> None:
        obj = o if isinstance(o, Literal) else URIRef(o)
        self.ds.get_context(URIRef(graph_uri)).add((URIRef(s), URIRef(p), obj))

    def triples(self, graph_uri: str) -> set[tuple[str, str, str]]:
        return {(str(s), str(p), str(o)) for s, p, o in self.ds.get_context(URIRef(graph_uri))}


# --- map_iri -----------------------------------------------------------------


@pytest.mark.unit
def test_map_iri_moves_vocabulary_terms_to_fdp_o() -> None:
    assert map_iri(f"{OLD}FAIRDataPoint") == f"{NEW}FAIRDataPoint"
    assert map_iri(f"{OLD}metadataState") == f"{NEW}metadataState"


@pytest.mark.unit
def test_map_iri_moves_shape_iris_to_urn() -> None:
    assert map_iri(f"{OLD}MetaMetadataShape") == "urn:fdp-shape:meta-metadata"
    assert map_iri(f"{OLD}MetaMetadataShape/meta") == "urn:fdp-shape:meta-metadata/meta"
    assert map_iri(f"{OLD}LicenseDocumentShape") == "urn:fdp-shape:license-document"
    assert map_iri(f"{OLD}ResourceDefinitionShape") == "urn:fdp-shape:resource-definition"
    assert map_iri(f"{OLD}ChildLinkShape") == "urn:fdp-shape:child-link"


@pytest.mark.unit
def test_map_iri_ignores_unrelated_and_new_namespace() -> None:
    assert map_iri(f"{NEW}FAIRDataPoint") is None  # idempotence at term level
    assert map_iri("https://example.org/thing") is None
    assert map_iri("urn:fdp-shape:meta-metadata") is None


@pytest.mark.unit
def test_shape_urn_map_matches_the_module_constants() -> None:
    # vocab_migration keeps a literal copy of the shape IRIs (importing the
    # defining modules would drag heavy deps); pin the correspondence here.
    assert map_iri(f"{OLD}MetaMetadataShape") == META_SHAPE_IRI
    assert map_iri(f"{OLD}LicenseDocumentShape") == LICENSE_SHAPE_IRI
    assert map_iri(f"{OLD}ResourceDefinitionShape") == RD_SHAPE_IRI


# --- migrate_vocabulary ------------------------------------------------------


def _seed_old_store(adapter: _DatasetAdapter) -> None:
    # Root record typed with the old class + old membership relation.
    adapter.add(BASE, BASE, str(RDF.type), f"{OLD}FAIRDataPoint")
    adapter.add(BASE, BASE, f"{OLD}servesMetadata", f"{BASE}/catalog/x")
    # A record's meta graph with the old state predicate.
    adapter.add(
        f"{BASE}/catalog/x/meta", f"{BASE}/catalog/x", f"{OLD}metadataState", Literal("PUBLISHED")
    )
    # The meta-metadata shape graph named under the old namespace, plus sibling.
    adapter.add(
        f"{OLD}MetaMetadataShape",
        f"{OLD}MetaMetadataShape",
        "http://www.w3.org/ns/shacl#path",
        f"{OLD}metadataState",
    )
    adapter.add(
        f"{OLD}MetaMetadataShape/meta",
        f"{OLD}MetaMetadataShape",
        f"{OLD}metadataState",
        Literal("PUBLISHED"),
    )
    # A graph with no old-namespace term at all.
    adapter.add(f"{BASE}/catalog/clean", f"{BASE}/catalog/clean", str(RDF.type), f"{NEW}Metadata")


@pytest.mark.unit
async def test_migrate_rewrites_terms_renames_shapes_and_backfills_root() -> None:
    adapter = _DatasetAdapter()
    _seed_old_store(adapter)

    report = await migrate_vocabulary(adapter=adapter, root_iri=BASE)  # type: ignore[arg-type]

    assert report.changed
    # Root: new class + backfilled MetadataService, membership relation moved.
    root = adapter.triples(BASE)
    assert (BASE, str(RDF.type), str(FDP_FAIRDATAPOINT)) in root
    assert (BASE, str(RDF.type), str(FDP_METADATA_SERVICE)) in root
    assert (BASE, f"{NEW}servesMetadata", f"{BASE}/catalog/x") in root
    assert not any(OLD in term for triple in root for term in triple)
    # Meta graph predicate rewritten in place.
    assert (
        f"{BASE}/catalog/x",
        str(FDP_METADATA_STATE),
        "PUBLISHED",
    ) in adapter.triples(f"{BASE}/catalog/x/meta")
    # Shape graph renamed to its urn, content rewritten, old graphs dropped.
    shape = adapter.triples("urn:fdp-shape:meta-metadata")
    assert (
        "urn:fdp-shape:meta-metadata",
        "http://www.w3.org/ns/shacl#path",
        f"{NEW}metadataState",
    ) in shape
    assert adapter.triples(f"{OLD}MetaMetadataShape") == set()
    assert adapter.triples(f"{OLD}MetaMetadataShape/meta") == set()
    sibling = adapter.triples("urn:fdp-shape:meta-metadata/meta")
    assert ("urn:fdp-shape:meta-metadata", str(FDP_METADATA_STATE), "PUBLISHED") in sibling
    # Untouched graph stays untouched.
    assert (f"{BASE}/catalog/clean", str(RDF.type), f"{NEW}Metadata") in adapter.triples(
        f"{BASE}/catalog/clean"
    )
    assert report.root_backfilled
    assert dict(report.renamed) == {
        f"{OLD}MetaMetadataShape": "urn:fdp-shape:meta-metadata",
        f"{OLD}MetaMetadataShape/meta": "urn:fdp-shape:meta-metadata/meta",
    }


@pytest.mark.unit
async def test_migrate_is_idempotent() -> None:
    adapter = _DatasetAdapter()
    _seed_old_store(adapter)
    first = await migrate_vocabulary(adapter=adapter, root_iri=BASE)  # type: ignore[arg-type]
    assert first.changed
    second = await migrate_vocabulary(adapter=adapter, root_iri=BASE)  # type: ignore[arg-type]
    assert not second.changed


@pytest.mark.unit
async def test_migrate_dry_run_reports_but_writes_nothing() -> None:
    adapter = _DatasetAdapter()
    _seed_old_store(adapter)
    before = {
        name: adapter.triples(name)
        for name in (BASE, f"{BASE}/catalog/x/meta", f"{OLD}MetaMetadataShape")
    }

    report = await migrate_vocabulary(adapter=adapter, root_iri=BASE, dry_run=True)  # type: ignore[arg-type]

    assert report.changed
    assert report.dry_run
    for name, triples in before.items():
        assert adapter.triples(name) == triples


@pytest.mark.unit
async def test_migrate_noop_on_clean_store() -> None:
    adapter = _DatasetAdapter()
    adapter.add(BASE, BASE, str(RDF.type), str(FDP_FAIRDATAPOINT))
    adapter.add(BASE, BASE, str(RDF.type), str(FDP_METADATA_SERVICE))
    report = await migrate_vocabulary(adapter=adapter, root_iri=BASE)  # type: ignore[arg-type]
    assert not report.changed


@pytest.mark.unit
async def test_migrate_backfills_metadata_service_on_otherwise_clean_root() -> None:
    # A deployment whose data was already re-typed (e.g. by hand) but lacks
    # the MetadataService assertion index validators need.
    adapter = _DatasetAdapter()
    adapter.add(BASE, BASE, str(RDF.type), str(FDP_FAIRDATAPOINT))
    report = await migrate_vocabulary(adapter=adapter, root_iri=BASE)  # type: ignore[arg-type]
    assert report.root_backfilled
    assert (BASE, str(RDF.type), str(FDP_METADATA_SERVICE)) in adapter.triples(BASE)
