"""Integration test: dump → restore round trip against a real Oxigraph store.

Verifies that the N-Quads produced by ``fdp backup dump`` reload faithfully via
``fdp backup restore`` through the real SPARQL 1.1 backend — the store's own
serializer output must parse back through the restore path, blank nodes included.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import HttpUrl
from rdflib import BNode, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdp.config import TripleStoreSettings
from fdp.metadata.backup import dump_store, restore_store
from fdp.metadata.backup.dump import DATA_MODEL_ADR0019
from fdp.storage.triplestore.adapter import TURTLE, TripleStoreAdapter, construct_named_graph

OXIGRAPH_PORT = 7878
BASE = "http://localhost:8000"
CATALOG = f"{BASE}/catalog/c1"
DCAT_CATALOG = URIRef("http://www.w3.org/ns/dcat#Catalog")

_CATALOG_TTL = f"""\
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct: <http://purl.org/dc/terms/> .
<{CATALOG}> a dcat:Catalog ; dct:title "Round trip" ;
    dct:conformsTo <{BASE}/fdp-api/profiles/catalog> .
"""
_META_TTL = f"""\
@prefix prov: <http://www.w3.org/ns/prov#> .
<{CATALOG}> prov:wasGeneratedBy [ a prov:Activity ] .
"""
_PROFILE_TTL = f"""\
@prefix prof: <http://www.w3.org/ns/dx/prof/> .
<{BASE}/fdp-api/profiles/catalog> a prof:Profile .
"""


@pytest.fixture
def oxigraph_container() -> Iterator[DockerContainer]:
    container = (
        DockerContainer("oxigraph/oxigraph:latest")
        .with_exposed_ports(OXIGRAPH_PORT)
        .with_command("serve --bind 0.0.0.0:7878 --location /data")
        .waiting_for(LogMessageWaitStrategy("Listening").with_startup_timeout(60))
    )
    with container:
        yield container


@pytest.fixture
async def adapter(oxigraph_container: DockerContainer) -> AsyncIterator[TripleStoreAdapter]:
    host = oxigraph_container.get_container_host_ip()
    port = oxigraph_container.get_exposed_port(OXIGRAPH_PORT)
    base = f"http://{host}:{port}"
    settings = TripleStoreSettings(
        query_endpoint=HttpUrl(f"{base}/query"),
        update_endpoint=HttpUrl(f"{base}/update"),
        graph_store_endpoint=HttpUrl(f"{base}/store"),
    )
    async with TripleStoreAdapter.from_settings(settings) as a:
        yield a


@pytest.mark.integration
async def test_dump_restore_round_trip(adapter: TripleStoreAdapter, tmp_path: Path) -> None:
    await adapter.replace_graph(CATALOG, _CATALOG_TTL, mime=TURTLE)
    await adapter.replace_graph(f"{CATALOG}/meta", _META_TTL, mime=TURTLE)
    await adapter.replace_graph(f"{BASE}/fdp-api/profiles/catalog", _PROFILE_TTL, mime=TURTLE)

    dump = await dump_store(adapter, tmp_path, identifier_base=BASE, include_audit=False)
    assert dump.graph_count == 3
    assert dump.data_model_version == DATA_MODEL_ADR0019  # profile graph present

    # Wipe the store, then restore from the dump.
    await adapter.clear_all()
    assert await adapter.ask("ASK { GRAPH ?g { ?s ?p ?o } }") is False

    restore = await restore_store(adapter, tmp_path, target_identifier_base=BASE)
    assert restore.graphs_loaded == 3
    assert restore.needs_migration is False

    # Content survives, including the record-schema binding and the blank node.
    catalog = await construct_named_graph(adapter, CATALOG)
    assert (URIRef(CATALOG), RDF.type, DCAT_CATALOG) in catalog
    assert (
        URIRef(CATALOG),
        DCTERMS.conformsTo,
        URIRef(f"{BASE}/fdp-api/profiles/catalog"),
    ) in catalog
    meta = await construct_named_graph(adapter, f"{CATALOG}/meta")
    activities = list(meta.objects(URIRef(CATALOG), PROV.wasGeneratedBy))
    assert len(activities) == 1
    assert isinstance(activities[0], BNode)
