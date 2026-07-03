"""Contract: record responses carry a valid RFC 8288 ``Link`` header.

FAIR Signposting (ADR-0017 §2) rides the same ``Link`` header as the LDP type /
``constrainedBy`` links. This asserts the wire contract a machine agent depends
on: the header parses as RFC 8288 and carries exactly one ``cite-as``.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef

from fdp.identity.deps import current_context
from fdp.metadata.ldp import build_ldp_router
from fdp.metadata.repository import MetadataRepository
from fdp.shared.errors import register_exception_handlers
from fdp.shared.namespaces import DCT, OWL
from tests.unit.metadata.ldp.test_router import FakeAdapter, FakePDP, _authenticated_ctx

RECORD_IRI = "http://testserver/ldp/catalogs/c1"
RECORD_PATH = "/ldp/catalogs/c1"


def _app(repo: MetadataRepository) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_ldp_router(repo=repo, pdp=FakePDP()))  # type: ignore[arg-type]
    app.dependency_overrides[current_context] = _authenticated_ctx
    return app


def _parse_link_header(header: str) -> list[tuple[str, dict[str, str]]]:
    """Parse an RFC 8288 ``Link`` header, asserting each member is well-formed."""
    entries: list[tuple[str, dict[str, str]]] = []
    for raw in (part.strip() for part in header.split(", ")):
        assert raw.startswith("<") and ">" in raw, f"not an RFC 8288 link: {raw!r}"
        target = raw[1 : raw.index(">")]
        params = dict(re.findall(r';\s*([A-Za-z-]+)="([^"]*)"', raw))
        assert "rel" in params, f"link missing rel: {raw!r}"
        entries.append((target, params))
    return entries


@pytest.mark.unit
async def test_record_link_header_is_valid_rfc8288_with_one_cite_as() -> None:
    adapter = FakeAdapter()
    repo = MetadataRepository(adapter)  # type: ignore[arg-type]
    graph = Graph()
    graph.add((URIRef(RECORD_IRI), DCT.title, Literal("Published catalog")))
    graph.add((URIRef(RECORD_IRI), OWL.sameAs, URIRef("https://doi.org/10.1234/foo")))
    await repo.put_graph(RECORD_IRI, graph, subject="urn:steward")

    with TestClient(_app(repo)) as client:
        resp = client.get(RECORD_PATH, headers={"Accept": "text/turtle"})

    assert resp.status_code == 200
    # httpx exposes a real RFC 8288 parser; a malformed header would break it.
    assert "cite-as" in resp.links
    entries = _parse_link_header(resp.headers["link"])
    cite_as = [target for target, params in entries if params["rel"] == "cite-as"]
    assert len(cite_as) == 1  # exactly one cite-as
    assert cite_as[0] == "https://doi.org/10.1234/foo"  # the client-supplied PID
    # Every link exposes a dereferenceable target + rel (contract for agents).
    assert all(target and params.get("rel") for target, params in entries)
