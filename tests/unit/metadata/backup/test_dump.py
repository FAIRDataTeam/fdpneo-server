"""Unit tests for ``fdp dump`` (ADR-0016 §2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF

from fdpneo_server.metadata.backup import dump_store
from fdpneo_server.metadata.backup.dump import (
    DATA_MODEL_ADR0019,
    DATA_MODEL_LEGACY,
    MANIFEST_FILE,
    RECORDS_FILE,
)

BASE = "http://localhost:8000"
DCAT = URIRef("http://www.w3.org/ns/dcat#Catalog")


@dataclass
class _FakeAdapter:
    """In-memory triple store: answers the graph-list SELECT and per-graph CONSTRUCT."""

    graphs: dict[str, Graph] = field(default_factory=dict)

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "SELECT DISTINCT ?g" in sparql:
            bindings = [{"g": {"value": g}} for g in self.graphs]
            return json.dumps({"results": {"bindings": bindings}}).encode()
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        graph = self.graphs.get(match.group(1), Graph()) if match else Graph()
        return graph.serialize(format="turtle").encode()


def _record_graph(iri: str) -> Graph:
    g = Graph()
    g.add((URIRef(iri), RDF.type, DCAT))
    g.add((URIRef(iri), DCTERMS.title, Literal("A record")))
    return g


def _meta_graph(iri: str) -> Graph:
    """A meta graph with a blank-node PROV activity (blank nodes must stay distinct)."""
    g = Graph()
    activity = BNode()
    g.add((URIRef(iri), PROV.wasGeneratedBy, activity))
    g.add((activity, RDF.type, PROV.Activity))
    return g


async def test_dump_writes_records_and_manifest(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        {
            f"{BASE}/catalog/c1": _record_graph(f"{BASE}/catalog/c1"),
            f"{BASE}/catalog/c1/meta": _meta_graph(f"{BASE}/catalog/c1"),
        }
    )
    result = await dump_store(adapter, tmp_path, identifier_base=BASE, include_audit=False)  # type: ignore[arg-type]

    assert (tmp_path / RECORDS_FILE).exists()
    manifest = json.loads((tmp_path / MANIFEST_FILE).read_text())
    assert manifest["identifier_base"] == BASE
    assert manifest["graph_count"] == 2
    assert manifest["quad_count"] == result.quad_count == 4
    assert manifest["data_model_version"] == DATA_MODEL_LEGACY  # no profile graph
    assert manifest["files"][RECORDS_FILE]  # sha256 present


async def test_dump_roundtrips_graphs_and_keeps_blank_nodes_distinct(tmp_path: Path) -> None:
    # Two records, each with a blank node in its meta graph. N-Quads scopes blank
    # nodes to the whole document, so the two must NOT collide on reload.
    adapter = _FakeAdapter(
        {
            f"{BASE}/catalog/c1/meta": _meta_graph(f"{BASE}/catalog/c1"),
            f"{BASE}/dataset/d1/meta": _meta_graph(f"{BASE}/dataset/d1"),
        }
    )
    await dump_store(adapter, tmp_path, identifier_base=BASE, include_audit=False)  # type: ignore[arg-type]

    # Text-based checks (rdflib's Dataset parser is deprecation-noisy). Each
    # N-Quads line is `S P O <graph> .`; the graph is the token before the final `.`.
    lines = [ln for ln in (tmp_path / RECORDS_FILE).read_text().splitlines() if ln.strip()]
    graphs = {ln.rsplit(" ", 2)[-2] for ln in lines}
    assert graphs == {f"<{BASE}/catalog/c1/meta>", f"<{BASE}/dataset/d1/meta>"}
    # Each activity blank node is unique to its graph — no cross-graph merge.
    bnodes = {tok for ln in lines for tok in ln.split() if tok.startswith("_:")}
    assert len(bnodes) == 2


async def test_dump_flags_adr0019_data_model_when_profiles_present(tmp_path: Path) -> None:
    profile = Graph()
    svc = BNode()
    profile.add((URIRef(f"{BASE}/fdp-api/profiles/catalog"), RDF.type, PROV.Entity))
    profile.add((URIRef(f"{BASE}/fdp-api/profiles/catalog"), DCTERMS.hasPart, svc))
    adapter = _FakeAdapter(
        {
            f"{BASE}/catalog/c1": _record_graph(f"{BASE}/catalog/c1"),
            f"{BASE}/fdp-api/profiles/catalog": profile,
        }
    )
    result = await dump_store(adapter, tmp_path, identifier_base=BASE, include_audit=False)  # type: ignore[arg-type]
    assert result.data_model_version == DATA_MODEL_ADR0019


async def test_dump_skips_empty_graphs(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        {
            f"{BASE}/catalog/c1": _record_graph(f"{BASE}/catalog/c1"),
            f"{BASE}/empty": Graph(),
        }
    )
    result = await dump_store(adapter, tmp_path, identifier_base=BASE, include_audit=False)  # type: ignore[arg-type]
    assert result.graph_count == 1  # the empty graph is not written
