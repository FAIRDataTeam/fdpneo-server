"""Unit tests for PROF conformance profiles (ADR-0019)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.prof import (
    ProfileService,
    build_profile_graph,
    build_profile_router,
    ensure_conformance,
    provision_profile,
)
from fdp.shared.errors import NotFound, register_exception_handlers
from fdp.shared.graphs import (
    meta_graph_uri,
    profile_graph_uri,
    profile_version_graph_uri,
    record_graph_uri,
    schema_graph_uri,
    schema_version_graph_uri,
)
from fdp.shared.namespaces import OWL, PROF, ROLE, SH

BASE = "http://localhost:8000"


@dataclass
class _Store:
    graphs: dict[str, Graph] = field(default_factory=dict)

    # repository
    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    async def get_meta(self, record_uri: str) -> Graph:
        return self.graphs.get(str(meta_graph_uri(record_uri)), Graph())

    # adapter
    async def replace_graph(self, graph_uri: str, data: str, *, mime: str = "") -> None:
        del mime
        g = Graph()
        g.parse(data=data, format="nt")
        self.graphs[graph_uri] = g

    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        bindings: list[dict[str, dict[str, str]]] = []
        for iri, g in self.graphs.items():
            if not iri.startswith(f"{BASE}/fdp-api/profiles/"):
                continue
            if (URIRef(iri), RDF.type, PROF.Profile) not in g:
                continue
            row: dict[str, dict[str, str]] = {"g": {"value": iri}}
            art = next(iter(g.objects(None, PROF.hasArtifact)), None)
            if art is not None:
                row["artifact"] = {"value": str(art)}
            bindings.append(row)
        _ = sparql
        return json.dumps({"results": {"bindings": bindings}}).encode()


def _service(store: _Store) -> ProfileService:
    return ProfileService(repository=store, adapter=store, base_url=BASE)  # type: ignore[arg-type]


@pytest.mark.unit
def test_build_profile_graph_has_validation_resource() -> None:
    profile = str(profile_graph_uri(BASE, "dataset"))
    artifact = str(schema_version_graph_uri(BASE, "dataset", "3"))
    g = build_profile_graph(profile, artifact)
    assert (URIRef(profile), RDF.type, PROF.Profile) in g
    res = next(g.objects(URIRef(profile), PROF.hasResource))
    assert (res, PROF.hasRole, ROLE.validation) in g
    assert (res, PROF.hasArtifact, URIRef(artifact)) in g


@pytest.mark.unit
async def test_provision_writes_stable_and_snapshot_pointing_at_schema_version() -> None:
    store = _Store()
    stable = await provision_profile(store, base_url=BASE, slug="dataset", version=3)  # type: ignore[arg-type]
    assert stable == str(profile_graph_uri(BASE, "dataset"))
    snapshot = str(profile_version_graph_uri(BASE, "dataset", "3"))
    artifact = URIRef(str(schema_version_graph_uri(BASE, "dataset", "3")))
    assert stable in store.graphs and snapshot in store.graphs
    # Both the stable and the snapshot point their validation resource at the
    # immutable schema version snapshot.
    for iri in (stable, snapshot):
        g = store.graphs[iri]
        assert (URIRef(iri), RDF.type, PROF.Profile) in g
        assert artifact in set(g.objects(None, PROF.hasArtifact))


@pytest.mark.unit
async def test_provision_is_idempotent_and_moves_current() -> None:
    store = _Store()
    await provision_profile(store, base_url=BASE, slug="dataset", version=1)  # type: ignore[arg-type]
    await provision_profile(store, base_url=BASE, slug="dataset", version=2)  # type: ignore[arg-type]
    # Prior version snapshot is retained; the stable profile now points at v2.
    assert str(profile_version_graph_uri(BASE, "dataset", "1")) in store.graphs
    assert str(profile_version_graph_uri(BASE, "dataset", "2")) in store.graphs
    stable = store.graphs[str(profile_graph_uri(BASE, "dataset"))]
    assert URIRef(str(schema_version_graph_uri(BASE, "dataset", "2"))) in set(
        stable.objects(None, PROF.hasArtifact)
    )


@pytest.mark.unit
async def test_service_get_current_version_and_404() -> None:
    store = _Store()
    svc = _service(store)
    await provision_profile(store, base_url=BASE, slug="dataset", version=1)  # type: ignore[arg-type]
    assert "Profile" in await svc.get_turtle("dataset")
    assert "Profile" in await svc.get_turtle("dataset", version="1")
    with pytest.raises(NotFound):
        await svc.get_turtle("dataset", version="99")
    with pytest.raises(NotFound):
        await svc.get_turtle("missing")


@pytest.mark.unit
async def test_service_list_excludes_version_snapshots() -> None:
    store = _Store()
    svc = _service(store)
    await provision_profile(store, base_url=BASE, slug="dataset", version=1)  # type: ignore[arg-type]
    await provision_profile(store, base_url=BASE, slug="catalog", version=1)  # type: ignore[arg-type]
    infos = await svc.list_profiles()
    assert {p.id for p in infos} == {"catalog", "dataset"}  # no "1" snapshots
    dataset = next(p for p in infos if p.id == "dataset")
    assert dataset.validation_artifact == str(schema_version_graph_uri(BASE, "dataset", "1"))


@pytest.mark.unit
async def test_ensure_conformance_fast_path_reads_existing_profile() -> None:
    store = _Store()
    await provision_profile(store, base_url=BASE, slug="dataset", version=2)  # type: ignore[arg-type]
    schema = str(schema_graph_uri(BASE, "dataset"))
    resolved = await ensure_conformance(store, store, schema_iri=schema)  # type: ignore[arg-type]
    assert resolved == (
        str(profile_graph_uri(BASE, "dataset")),
        str(profile_version_graph_uri(BASE, "dataset", "2")),
    )


@pytest.mark.unit
async def test_ensure_conformance_lazy_provisions_from_bootstrap_schema() -> None:
    store = _Store()
    # A schema seeded at bootstrap: stable shape graph + meta versionInfo, no profile.
    schema = str(schema_graph_uri(BASE, "catalog"))
    shape = Graph()
    shape.add((URIRef(schema), RDF.type, SH.NodeShape))
    store.graphs[schema] = shape
    meta = Graph()
    meta.add((URIRef(schema), OWL.versionInfo, Literal(1)))
    store.graphs[str(meta_graph_uri(schema))] = meta

    resolved = await ensure_conformance(store, store, schema_iri=schema)  # type: ignore[arg-type]
    assert resolved == (
        str(profile_graph_uri(BASE, "catalog")),
        str(profile_version_graph_uri(BASE, "catalog", "1")),
    )
    # It self-healed: snapshotted the schema version and provisioned the profile.
    assert str(schema_version_graph_uri(BASE, "catalog", "1")) in store.graphs
    assert str(profile_graph_uri(BASE, "catalog")) in store.graphs


@pytest.mark.unit
async def test_ensure_conformance_none_for_external_or_empty_schema() -> None:
    store = _Store()
    # External (non-managed) shape IRI → no derived profile.
    assert await ensure_conformance(store, store, schema_iri="http://ex.org/shape") is None  # type: ignore[arg-type]
    # Managed IRI but no stored shape → nothing to wrap.
    ghost = str(schema_graph_uri(BASE, "ghost"))
    store.graphs[str(meta_graph_uri(ghost))] = Graph()
    assert await ensure_conformance(store, store, schema_iri=ghost) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_router_reads_are_public() -> None:
    store = _Store()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_profile_router(service=_service(store)))
    client = TestClient(app)
    # Empty list is fine and unauthenticated.
    resp = client.get("/profiles")
    assert resp.status_code == 200
    assert resp.json() == {"profiles": []}
    assert client.get("/profiles/missing").status_code == 404
