"""Integration test: :class:`MetadataRepository` against a real Oxigraph.

Exercises the full lifecycle so the repository is verified against an
actual SPARQL 1.1 backend rather than only the in-memory fake adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from pydantic import HttpUrl
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdp.config import TripleStoreSettings
from fdp.metadata.graphs import audit_graph_uri, meta_graph_uri
from fdp.metadata.repository import MetadataRepository
from fdp.shared.namespaces import DCT, OWL, PROV
from fdp.storage.triplestore import TripleStoreAdapter

OXIGRAPH_PORT = 7878
RECORD = "https://example.org/records/integration-r1"
RECORD_URI = URIRef(RECORD)
ALICE = "https://idp.example/realms/fdp#alice"


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
async def repo(oxigraph_container: DockerContainer) -> AsyncIterator[MetadataRepository]:
    host = oxigraph_container.get_container_host_ip()
    port = oxigraph_container.get_exposed_port(OXIGRAPH_PORT)
    base = f"http://{host}:{port}"
    settings = TripleStoreSettings(
        query_endpoint=HttpUrl(f"{base}/query"),
        update_endpoint=HttpUrl(f"{base}/update"),
        graph_store_endpoint=HttpUrl(f"{base}/store"),
    )
    async with TripleStoreAdapter.from_settings(settings) as adapter:
        yield MetadataRepository(adapter)


def _record_graph(title: str = "hello") -> Graph:
    g = Graph()
    g.add((RECORD_URI, DCT.title, Literal(title)))
    return g


@pytest.mark.integration
async def test_full_lifecycle(repo: MetadataRepository) -> None:
    # Empty to start.
    assert len(await repo.get_graph(RECORD)) == 0

    etag1 = await repo.put_graph(RECORD, _record_graph("hello"), creator=ALICE)
    fetched = await repo.get_graph(RECORD)
    assert (RECORD_URI, DCT.title, Literal("hello")) in fetched

    # Meta graph carries creator + version 1 + PROV.Entity type.
    meta1 = Graph()
    meta_body = await _construct_named_graph(repo, meta_graph_uri(RECORD))
    meta1.parse(data=meta_body, format="turtle")
    assert (RECORD_URI, RDF.type, PROV.Entity) in meta1
    assert (RECORD_URI, DCT.creator, URIRef(ALICE)) in meta1
    assert (RECORD_URI, OWL.versionInfo, Literal(1)) in meta1

    # PATCH inserts a description; meta version becomes 2.
    update = (
        f"INSERT DATA {{ GRAPH <{RECORD}> "
        "{ <https://example.org/records/integration-r1> "
        '<http://purl.org/dc/terms/description> "added" } }'
    )
    etag2 = await repo.patch_graph(RECORD, update, creator=ALICE)
    assert etag1 != etag2
    meta2 = Graph()
    meta2.parse(
        data=await _construct_named_graph(repo, meta_graph_uri(RECORD)),
        format="turtle",
    )
    assert (RECORD_URI, OWL.versionInfo, Literal(2)) in meta2

    # Audit graph populated externally — make sure delete removes it too.
    audit = Graph()
    audit.add((RECORD_URI, DCT.subject, Literal("anything")))
    await _replace_named_graph(repo, audit_graph_uri(RECORD), audit)

    await repo.delete_graph(RECORD)
    assert len(await repo.get_graph(RECORD)) == 0
    assert not (await _construct_named_graph(repo, meta_graph_uri(RECORD))).strip()
    assert not (await _construct_named_graph(repo, audit_graph_uri(RECORD))).strip()


async def _construct_named_graph(repo: MetadataRepository, uri: URIRef) -> str:
    sparql = f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{uri}> {{ ?s ?p ?o }} }}"
    body = await repo._adapter.query(sparql, accept="text/turtle")  # pyright: ignore[reportPrivateUsage]
    return body.decode("utf-8")


async def _replace_named_graph(repo: MetadataRepository, uri: URIRef, graph: Graph) -> None:
    nt = graph.serialize(format="nt")
    await repo._adapter.replace_graph(  # pyright: ignore[reportPrivateUsage]
        str(uri), nt, mime="application/n-triples"
    )
