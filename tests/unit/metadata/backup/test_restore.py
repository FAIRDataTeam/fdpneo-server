"""Unit tests for ``fdp backup restore`` (ADR-0016 §3)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF

from fdp.metadata.backup import RestoreError, dump_store, restore_store
from fdp.metadata.backup.dump import RECORDS_FILE

BASE = "http://localhost:8000"
DCAT_CATALOG = URIRef("http://www.w3.org/ns/dcat#Catalog")


@dataclass
class _Store:
    """In-memory triple store: graph-list SELECT, per-graph CONSTRUCT, replace_graph."""

    graphs: dict[str, Graph] = field(default_factory=dict)

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "SELECT DISTINCT ?g" in sparql:
            bindings = [{"g": {"value": g}} for g in self.graphs]
            return json.dumps({"results": {"bindings": bindings}}).encode()
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        graph = self.graphs.get(match.group(1), Graph()) if match else Graph()
        return graph.serialize(format="turtle").encode()

    async def replace_graph(self, graph_uri: str, data: str | bytes, *, mime: str = "") -> None:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        fmt = "nt" if "n-triples" in mime else "turtle"
        graph = Graph()
        graph.parse(data=text, format=fmt)
        self.graphs[graph_uri] = graph


def _record(iri: str) -> Graph:
    g = Graph()
    g.add((URIRef(iri), RDF.type, DCAT_CATALOG))
    g.add((URIRef(iri), DCTERMS.title, Literal("A record")))
    return g


def _meta(iri: str) -> Graph:
    g = Graph()
    activity = BNode()
    g.add((URIRef(iri), PROV.wasGeneratedBy, activity))
    g.add((activity, RDF.type, PROV.Activity))
    return g


def _source(*, with_profile: bool = True) -> _Store:
    graphs = {
        f"{BASE}/catalog/c1": _record(f"{BASE}/catalog/c1"),
        f"{BASE}/catalog/c1/meta": _meta(f"{BASE}/catalog/c1"),
    }
    if with_profile:
        prof = Graph()
        prof.add((URIRef(f"{BASE}/fdp-api/profiles/catalog"), RDF.type, PROV.Entity))
        graphs[f"{BASE}/fdp-api/profiles/catalog"] = prof
    return _Store(graphs)


async def _dump(tmp_path: Path, source: _Store) -> Path:
    await dump_store(source, tmp_path, identifier_base=BASE, include_audit=False)  # type: ignore[arg-type]
    return tmp_path


async def test_restore_roundtrips_every_graph(tmp_path: Path) -> None:
    source = _source()
    await _dump(tmp_path, source)
    target = _Store()

    result = await restore_store(target, tmp_path, target_identifier_base=BASE)  # type: ignore[arg-type]

    assert result.graphs_loaded == 3
    assert set(target.graphs) == set(source.graphs)
    # Record content survives.
    catalog = target.graphs[f"{BASE}/catalog/c1"]
    assert (URIRef(f"{BASE}/catalog/c1"), RDF.type, DCAT_CATALOG) in catalog
    # The meta graph's blank-node activity survives as exactly one blank node.
    meta = target.graphs[f"{BASE}/catalog/c1/meta"]
    activities = list(meta.objects(URIRef(f"{BASE}/catalog/c1"), PROV.wasGeneratedBy))
    assert len(activities) == 1
    assert isinstance(activities[0], BNode)
    # ADR-0019 dump → no migration needed.
    assert result.needs_migration is False


async def test_restore_flags_migration_for_legacy_dump(tmp_path: Path) -> None:
    await _dump(tmp_path, _source(with_profile=False))
    result = await restore_store(_Store(), tmp_path, target_identifier_base=BASE)  # type: ignore[arg-type]
    assert result.needs_migration is True


async def test_restore_refuses_base_mismatch(tmp_path: Path) -> None:
    await _dump(tmp_path, _source())
    with pytest.raises(RestoreError, match="identifier_base mismatch"):
        await restore_store(_Store(), tmp_path, target_identifier_base="http://other.example")  # type: ignore[arg-type]


async def test_restore_refuses_nonempty_store(tmp_path: Path) -> None:
    await _dump(tmp_path, _source())
    target = _Store({f"{BASE}/existing": _record(f"{BASE}/existing")})
    with pytest.raises(RestoreError, match="not empty"):
        await restore_store(target, tmp_path, target_identifier_base=BASE)  # type: ignore[arg-type]


async def test_restore_merge_skips_existing_graphs(tmp_path: Path) -> None:
    await _dump(tmp_path, _source())
    # Target already has the catalog graph; --merge skips it, loads the rest.
    target = _Store({f"{BASE}/catalog/c1": _record(f"{BASE}/catalog/c1")})
    result = await restore_store(target, tmp_path, target_identifier_base=BASE, merge=True)  # type: ignore[arg-type]
    assert result.graphs_skipped == 1
    assert result.graphs_loaded == 2


async def test_restore_dry_run_writes_nothing(tmp_path: Path) -> None:
    await _dump(tmp_path, _source())
    target = _Store()
    result = await restore_store(target, tmp_path, target_identifier_base=BASE, dry_run=True)  # type: ignore[arg-type]
    assert result.dry_run is True
    assert result.graphs_loaded == 3  # reported…
    assert target.graphs == {}  # …but nothing written


async def test_restore_detects_corrupt_records(tmp_path: Path) -> None:
    await _dump(tmp_path, _source())
    (tmp_path / RECORDS_FILE).write_text("<x> <y> <z> <g> .\n", encoding="utf-8")  # checksum breaks
    with pytest.raises(RestoreError, match="checksum mismatch"):
        await restore_store(_Store(), tmp_path, target_identifier_base=BASE)  # type: ignore[arg-type]
