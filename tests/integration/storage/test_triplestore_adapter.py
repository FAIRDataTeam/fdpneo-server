"""Integration test: TripleStoreAdapter against a real Oxigraph container.

Exercises the full read/write lifecycle so the adapter is verified against
the actual SPARQL 1.1 Protocol implementation rather than only mocks.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import pytest
from pydantic import HttpUrl
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from fdp.config import TripleStoreSettings
from fdp.storage.triplestore.adapter import TURTLE, TripleStoreAdapter

OXIGRAPH_PORT = 7878
GRAPH_URI = "http://example.org/g/test"
TURTLE_PAYLOAD = """\
@prefix ex: <http://example.org/> .
ex:a ex:knows ex:b .
ex:b ex:knows ex:c .
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
async def test_ingest_query_ask_replace_drop_round_trip(adapter: TripleStoreAdapter) -> None:
    # Empty to start.
    assert await adapter.ask(f"ASK {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}") is False

    # Ingest two triples.
    await adapter.ingest_graph(GRAPH_URI, TURTLE_PAYLOAD)

    # SELECT them back via JSON results.
    body = await adapter.query(
        f"SELECT ?s ?o WHERE {{ GRAPH <{GRAPH_URI}> {{ ?s <http://example.org/knows> ?o }} }} ORDER BY ?s"
    )
    payload = json.loads(body)
    bindings = payload["results"]["bindings"]
    assert len(bindings) == 2
    assert bindings[0]["s"]["value"] == "http://example.org/a"
    assert bindings[1]["o"]["value"] == "http://example.org/c"

    # ASK now returns True.
    assert await adapter.ask(f"ASK {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}") is True

    # CONSTRUCT round-trip via Turtle accept header.
    construct_body = await adapter.query(
        f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}",
        accept=TURTLE,
    )
    assert b"example.org/a" in construct_body

    # SPARQL Update inserts a third triple.
    await adapter.update(
        f"INSERT DATA {{ GRAPH <{GRAPH_URI}> "
        "{ <http://example.org/c> <http://example.org/knows> <http://example.org/d> . } }"
    )
    body = await adapter.query(
        f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}"
    )
    count = int(json.loads(body)["results"]["bindings"][0]["n"]["value"])
    assert count == 3

    # Replace the graph: only the new payload remains.
    replacement = b"<http://example.org/x> <http://example.org/y> <http://example.org/z> ."
    await adapter.replace_graph(GRAPH_URI, replacement, mime="application/n-triples")
    body = await adapter.query(f"SELECT ?s WHERE {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}")
    bindings = json.loads(body)["results"]["bindings"]
    assert [b["s"]["value"] for b in bindings] == ["http://example.org/x"]

    # Drop the graph.
    await adapter.drop_graph(GRAPH_URI)
    assert await adapter.ask(f"ASK {{ GRAPH <{GRAPH_URI}> {{ ?s ?p ?o }} }}") is False
