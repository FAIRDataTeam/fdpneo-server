"""Unit tests for the SPARQL triple store adapter (transport mocked with respx)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import HttpUrl, SecretStr

from fdp.config import TripleStoreSettings
from fdp.storage.triplestore.adapter import (
    SPARQL_JSON,
    SPARQL_UPDATE,
    TURTLE,
    TripleStoreAdapter,
)

QUERY_URL = "http://triplestore.local/query"
UPDATE_URL = "http://triplestore.local/update"
GRAPH_STORE_URL = "http://triplestore.local/rdf-graphs/service"


def _settings(*, with_graph_store: bool = True, with_auth: bool = False) -> TripleStoreSettings:
    return TripleStoreSettings(
        query_endpoint=HttpUrl(QUERY_URL),
        update_endpoint=HttpUrl(UPDATE_URL),
        graph_store_endpoint=HttpUrl(GRAPH_STORE_URL) if with_graph_store else None,
        username="alice" if with_auth else None,
        password=SecretStr("s3cret") if with_auth else None,
    )


def _adapter(settings: TripleStoreSettings, client: httpx.AsyncClient) -> TripleStoreAdapter:
    return TripleStoreAdapter(settings, client)


@pytest.fixture
def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@pytest.mark.unit
@respx.mock
async def test_query_posts_form_encoded_with_accept_header(
    async_client: httpx.AsyncClient,
) -> None:
    route = respx.post(QUERY_URL).respond(
        200,
        json={"results": {"bindings": [{"s": {"value": "x"}}]}},
        headers={"Content-Type": SPARQL_JSON},
    )
    body = await _adapter(_settings(), async_client).query("SELECT ?s WHERE { ?s ?p ?o }")
    assert json.loads(body)["results"]["bindings"] == [{"s": {"value": "x"}}]

    sent = route.calls.last.request
    assert sent.headers["accept"] == SPARQL_JSON
    assert b"query=SELECT" in sent.content


@pytest.mark.unit
@respx.mock
async def test_query_propagates_custom_accept_for_construct(
    async_client: httpx.AsyncClient,
) -> None:
    route = respx.post(QUERY_URL).respond(200, text="<a> <b> <c> .")
    await _adapter(_settings(), async_client).query("CONSTRUCT { ?s ?p ?o }", accept=TURTLE)
    assert route.calls.last.request.headers["accept"] == TURTLE


@pytest.mark.unit
@respx.mock
async def test_ask_returns_boolean(async_client: httpx.AsyncClient) -> None:
    respx.post(QUERY_URL).respond(200, json={"head": {}, "boolean": True})
    assert await _adapter(_settings(), async_client).ask("ASK { ?s ?p ?o }") is True


@pytest.mark.unit
@respx.mock
async def test_update_posts_application_sparql_update(
    async_client: httpx.AsyncClient,
) -> None:
    route = respx.post(UPDATE_URL).respond(204)
    await _adapter(_settings(), async_client).update("DELETE WHERE { ?s ?p ?o }")
    sent = route.calls.last.request
    assert sent.headers["content-type"] == SPARQL_UPDATE
    assert sent.content == b"DELETE WHERE { ?s ?p ?o }"


@pytest.mark.unit
@respx.mock
async def test_query_raises_on_http_error(async_client: httpx.AsyncClient) -> None:
    respx.post(QUERY_URL).respond(500, text="boom")
    with pytest.raises(httpx.HTTPStatusError):
        await _adapter(_settings(), async_client).query("SELECT * { ?s ?p ?o }")


@pytest.mark.unit
@respx.mock
async def test_ingest_graph_uses_graph_store_post(async_client: httpx.AsyncClient) -> None:
    route = respx.post(GRAPH_STORE_URL).respond(204)
    await _adapter(_settings(), async_client).ingest_graph(
        "http://example.org/g/1", "<a> <b> <c> ."
    )
    sent = route.calls.last.request
    assert sent.url.params["graph"] == "http://example.org/g/1"
    assert sent.headers["content-type"] == TURTLE
    assert sent.content == b"<a> <b> <c> ."


@pytest.mark.unit
@respx.mock
async def test_replace_graph_uses_graph_store_put(async_client: httpx.AsyncClient) -> None:
    route = respx.put(GRAPH_STORE_URL).respond(204)
    await _adapter(_settings(), async_client).replace_graph(
        "http://example.org/g/2", b"<a> <b> <c> ."
    )
    assert route.calls.last.request.url.params["graph"] == "http://example.org/g/2"


@pytest.mark.unit
async def test_ingest_graph_requires_graph_store_endpoint(
    async_client: httpx.AsyncClient,
) -> None:
    adapter = _adapter(_settings(with_graph_store=False), async_client)
    with pytest.raises(ValueError, match="graph_store_endpoint is required"):
        await adapter.ingest_graph("http://example.org/g/1", "<a> <b> <c> .")


@pytest.mark.unit
@respx.mock
async def test_drop_graph_uses_graph_store_delete_when_configured(
    async_client: httpx.AsyncClient,
) -> None:
    route = respx.delete(GRAPH_STORE_URL).respond(204)
    await _adapter(_settings(), async_client).drop_graph("http://example.org/g/9")
    assert route.calls.last.request.url.params["graph"] == "http://example.org/g/9"


@pytest.mark.unit
@respx.mock
async def test_drop_graph_falls_back_to_sparql_update_without_graph_store(
    async_client: httpx.AsyncClient,
) -> None:
    route = respx.post(UPDATE_URL).respond(204)
    await _adapter(_settings(with_graph_store=False), async_client).drop_graph(
        "http://example.org/g/9"
    )
    sent = route.calls.last.request
    assert sent.headers["content-type"] == SPARQL_UPDATE
    assert b"DROP SILENT GRAPH <http://example.org/g/9>" in sent.content


@pytest.mark.unit
@respx.mock
async def test_from_settings_attaches_basic_auth() -> None:
    route = respx.post(QUERY_URL).respond(200, json={"head": {}, "results": {"bindings": []}})
    async with TripleStoreAdapter.from_settings(_settings(with_auth=True)) as adapter:
        await adapter.query("SELECT * { ?s ?p ?o }")
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")
