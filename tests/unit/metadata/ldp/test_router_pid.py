"""LDP router persistent-identifier behavior (ADR-0014): canonicalization +
the dual identifier model, exercised through the real router with fakes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.identity.deps import current_context
from fdp.metadata.ldp import build_ldp_router
from fdp.metadata.repository import MetadataRepository
from fdp.shared.errors import register_exception_handlers
from fdp.shared.namespaces import DCAT, DCT, OWL
from tests.unit.metadata.ldp.test_router import (
    FakeAdapter,
    FakePDP,
    _authenticated_ctx,
)

ID_BASE = "https://w3id.org/myfdp"
SERVING = "http://testserver"


def _app(repo: MetadataRepository, pdp: FakePDP, containers: object | None = None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_ldp_router(
            repo=repo,  # type: ignore[arg-type]
            pdp=pdp,  # type: ignore[arg-type]
            containers=containers,  # type: ignore[arg-type]
            identifier_base=ID_BASE,
            serving_origins=[SERVING],
        )
    )
    app.dependency_overrides[current_context] = _authenticated_ctx
    return app


@pytest.mark.unit
async def test_put_stores_under_canonical_identifier_base() -> None:
    adapter = FakeAdapter()
    repo = MetadataRepository(adapter)  # type: ignore[arg-type]
    pdp = FakePDP()
    app = _app(repo, pdp)

    body = f'<> a <{DCAT.Dataset}> ; <{DCT.title}> "hi" .'
    with TestClient(app) as client:
        resp = client.put("/ldp/catalogs/c1", content=body, headers={"content-type": "text/turtle"})
    assert resp.status_code == 201
    canonical = f"{ID_BASE}/ldp/catalogs/c1"
    # Stored under the canonical IRI even though the request arrived on testserver.
    assert canonical in adapter.graphs
    assert resp.headers["location"] == canonical
    # Authorization saw the canonical IRI, not the request host.
    assert ("modify", canonical) in pdp.calls


@pytest.mark.unit
async def test_get_resolves_canonical_record_from_serving_host() -> None:
    adapter = FakeAdapter()
    repo = MetadataRepository(adapter)  # type: ignore[arg-type]
    canonical = f"{ID_BASE}/ldp/catalogs/c1"
    seed = Graph()
    seed.add((URIRef(canonical), DCT.title, URIRef("urn:x")))
    await repo.put_graph(canonical, seed, subject=None)

    app = _app(repo, FakePDP())
    with TestClient(app) as client:
        resp = client.get("/ldp/catalogs/c1")
    assert resp.status_code == 200
    assert "catalogs/c1" in resp.text


@pytest.mark.unit
async def test_foreign_subject_rebound_with_sameas() -> None:
    adapter = FakeAdapter()
    repo = MetadataRepository(adapter)  # type: ignore[arg-type]
    app = _app(repo, FakePDP())

    foreign = "https://doi.org/10.1234/foo"
    body = f'<{foreign}> a <{DCAT.Dataset}> ; <{DCT.title}> "brought along" .'
    with TestClient(app) as client:
        resp = client.put("/ldp/catalogs/c1", content=body, headers={"content-type": "text/turtle"})
    assert resp.status_code == 201
    canonical = f"{ID_BASE}/ldp/catalogs/c1"
    stored = adapter.graphs[canonical]
    canon = URIRef(canonical)
    # Subject rebound to canonical; foreign kept as owl:sameAs cross-reference.
    assert (canon, RDF.type, DCAT.Dataset) in stored
    assert (canon, OWL.sameAs, URIRef(foreign)) in stored
    assert (URIRef(foreign), RDF.type, DCAT.Dataset) not in stored
