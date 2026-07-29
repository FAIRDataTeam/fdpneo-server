"""Unit tests for the ADR-0019 conformance backfill."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdpneo_server.metadata.prof_backfill import backfill_conformance
from fdpneo_server.shared.graphs import (
    meta_graph_uri,
    profile_graph_uri,
    profile_version_graph_uri,
    record_graph_uri,
    schema_graph_uri,
    schema_version_graph_uri,
)
from fdpneo_server.shared.namespaces import DCT, FDP_VALIDATED_AGAINST, OWL, PROV, SH

BASE = "http://localhost:8000"


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)

    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    async def get_meta(self, record_uri: str) -> Graph:
        return self.graphs.get(str(meta_graph_uri(record_uri)), Graph())

    async def replace_graph(self, graph_uri: str, data: str, *, mime: str = "") -> None:
        del mime
        g = Graph()
        g.parse(data=data, format="nt")
        self.graphs[graph_uri] = g

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del sparql, accept
        bindings = [{"g": {"value": key}} for key in self.graphs]
        return json.dumps({"results": {"bindings": bindings}}).encode()


class _Cache:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def shape_for(self, resource_iri: str) -> str | None:
        return self._m.get(resource_iri)


def _seed_type(store: _Store, slug: str, record_iri: str) -> str:
    """Seed a managed schema (stable + meta v1) and a bare record of that type."""
    schema = str(schema_graph_uri(BASE, slug))
    shape = Graph()
    shape.add((URIRef(schema), RDF.type, SH.NodeShape))
    store.graphs[schema] = shape
    schema_meta = Graph()
    schema_meta.add((URIRef(schema), OWL.versionInfo, Literal(1)))
    store.graphs[str(meta_graph_uri(schema))] = schema_meta

    record = Graph()
    record.add((URIRef(record_iri), DCT.title, Literal("r")))
    store.graphs[record_iri] = record
    rec_meta = Graph()
    rec_meta.add((URIRef(record_iri), RDF.type, PROV.Entity))
    rec_meta.add((URIRef(record_iri), DCT.created, Literal(datetime(2026, 1, 1, tzinfo=UTC))))
    store.graphs[str(meta_graph_uri(record_iri))] = rec_meta
    return schema


@pytest.mark.unit
async def test_backfill_provisions_profiles_and_binds_records() -> None:
    store = _Store()
    record_iri = f"{BASE}/catalog/c1"
    schema = _seed_type(store, "catalog", record_iri)
    cache = _Cache({record_iri: schema})

    report = await backfill_conformance(adapter=store, repository=store, cache=cache)  # type: ignore[arg-type]

    assert report.changed
    assert str(profile_graph_uri(BASE, "catalog")) in report.profiles_provisioned
    assert record_iri in report.records_stamped
    # Profile + schema-version snapshot exist.
    assert str(profile_graph_uri(BASE, "catalog")) in store.graphs
    assert str(schema_version_graph_uri(BASE, "catalog", "1")) in store.graphs
    # Record self-describes; meta records the exact version.
    record = store.graphs[record_iri]
    assert (
        URIRef(record_iri),
        DCT.conformsTo,
        URIRef(profile_graph_uri(BASE, "catalog")),
    ) in record
    meta = store.graphs[str(meta_graph_uri(record_iri))]
    assert (
        URIRef(record_iri),
        FDP_VALIDATED_AGAINST,
        URIRef(profile_version_graph_uri(BASE, "catalog", "1")),
    ) in meta


@pytest.mark.unit
async def test_backfill_is_idempotent() -> None:
    store = _Store()
    record_iri = f"{BASE}/catalog/c1"
    schema = _seed_type(store, "catalog", record_iri)
    cache = _Cache({record_iri: schema})

    await backfill_conformance(adapter=store, repository=store, cache=cache)  # type: ignore[arg-type]
    second = await backfill_conformance(adapter=store, repository=store, cache=cache)  # type: ignore[arg-type]
    assert not second.changed
    assert second.already >= 1


@pytest.mark.unit
async def test_backfill_skips_records_without_a_known_type() -> None:
    store = _Store()
    # A record with no RD mapping (cache.shape_for → None) is left untouched.
    orphan = f"{BASE}/misc/x1"
    g = Graph()
    g.add((URIRef(orphan), DCT.title, Literal("x")))
    store.graphs[orphan] = g
    report = await backfill_conformance(adapter=store, repository=store, cache=_Cache({}))  # type: ignore[arg-type]
    assert not report.changed
    assert (URIRef(orphan), DCT.conformsTo, None) not in [
        (s, p, None) for s, p, _ in store.graphs[orphan]
    ]
