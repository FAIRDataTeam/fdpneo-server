"""Unit tests for the dump archive packaging (dump_to_archive / extract_archive)."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from fdp.metadata.backup.dump import MANIFEST_FILE, RECORDS_FILE
from fdp.metadata.backup.orchestrate import dump_to_archive, extract_archive

pytestmark = pytest.mark.unit

BASE = "http://localhost:8000"


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        if "SELECT DISTINCT ?g" in sparql:
            bindings = [{"g": {"value": g}} for g in self.graphs]
            return json.dumps({"results": {"bindings": bindings}}).encode()
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        graph = self.graphs.get(match.group(1), Graph()) if match else Graph()
        return graph.serialize(format="turtle").encode()


def _catalog() -> Graph:
    g = Graph()
    g.add((URIRef(f"{BASE}/catalog/c1"), RDF.type, URIRef("http://www.w3.org/ns/dcat#Catalog")))
    g.add((URIRef(f"{BASE}/catalog/c1"), DCTERMS.title, Literal("C")))
    return g


async def test_dump_to_archive_produces_zip_with_manifest_and_records(tmp_path: Path) -> None:
    store = _Store({f"{BASE}/catalog/c1": _catalog()})
    result, archive = await dump_to_archive(
        store,  # type: ignore[arg-type]
        identifier_base=BASE,
        work_dir=tmp_path,
        include_audit=False,
    )
    assert result.graph_count == 1
    assert archive.suffix == ".zip" and archive.exists()
    names = set(zipfile.ZipFile(archive).namelist())
    assert {RECORDS_FILE, MANIFEST_FILE} <= names


async def test_extract_archive_round_trips_dump_files(tmp_path: Path) -> None:
    store = _Store({f"{BASE}/catalog/c1": _catalog()})
    _, archive = await dump_to_archive(
        store,  # type: ignore[arg-type]
        identifier_base=BASE,
        work_dir=tmp_path,
        include_audit=False,
    )
    dest = tmp_path / "extracted"
    await extract_archive(archive, dest)
    assert (dest / RECORDS_FILE).exists()
    manifest = json.loads((dest / MANIFEST_FILE).read_text())
    assert manifest["identifier_base"] == BASE
    assert manifest["graph_count"] == 1
