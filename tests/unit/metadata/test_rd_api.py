"""Unit tests for the resource-definition catalog + admin router (#4).

Drives the router over a FastAPI app with a fake ResourceDefinitionService
and an in-memory cache, asserting: public reads, admin-role gating, create
uniqueness + reserved-prefix + schema-existence validation, replace (the
add-child-to-existing-type path), root-delete protection, and that the
on_rebuilt swap is reflected in subsequent reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdpneo_server.metadata.profiles import (
    ResourceDefinitionRecord,
    rd_record_slug,
    resolve_cache,
)
from fdpneo_server.metadata.profiles.registry import ResourceDefinitionCache
from fdpneo_server.metadata.rd_api import build_resource_definition_router
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import register_exception_handlers

BASE = "http://localhost:8000"
DCAT = "http://www.w3.org/ns/dcat#"

CATALOG = ResourceDefinitionRecord(
    url_prefix="catalog", name="Catalog", schema_iri=f"{DCAT}Catalog"
)
ROOT = ResourceDefinitionRecord(
    url_prefix="", name="Repository", schema_iri="https://w3id.org/fdp/o#Repository"
)


@dataclass
class _FakeService:
    """Stand-in for ResourceDefinitionService backed by an in-memory dict.

    ``put`` / ``delete`` mutate the dict and rebuild the cache the same way
    the real service does, so the router's post-mutation reads see the change.
    """

    records: dict[str, ResourceDefinitionRecord] = field(default_factory=dict)
    schemas: set[str] = field(default_factory=set)
    app: FastAPI | None = None
    put_calls: list[str] = field(default_factory=list)

    def cache(self) -> ResourceDefinitionCache:
        return resolve_cache(self.records.values(), base_url=BASE)

    def _publish(self) -> None:
        if self.app is not None:
            self.app.state.resource_definitions = self.cache()

    async def schema_exists(self, schema_iri: str) -> bool:
        return schema_iri in self.schemas

    async def put(
        self, record: ResourceDefinitionRecord, *, subject: str | None = None
    ) -> ResourceDefinitionCache:
        del subject
        self.put_calls.append(rd_record_slug(record.url_prefix, record.name))
        self.records[rd_record_slug(record.url_prefix, record.name)] = record
        self._publish()
        return self.cache()

    async def delete(self, record: ResourceDefinitionRecord) -> ResourceDefinitionCache:
        self.records.pop(rd_record_slug(record.url_prefix, record.name), None)
        self._publish()
        return self.cache()


def _build(service: _FakeService, *, ctx: RequestContext) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    service.app = app
    app.state.resource_definitions = service.cache()
    app.include_router(
        build_resource_definition_router(
            service=service,  # type: ignore[arg-type]
            cache_provider=lambda: app.state.resource_definitions,
            base_url=BASE,
        )
    )
    # Bind the request context the deps layer reads (no auth middleware here).
    app.dependency_overrides = {}

    from fdpneo_server.identity.deps import current_context

    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


def _admin() -> RequestContext:
    return RequestContext(
        subject="https://idp/realms/fdp#admin",
        roles=frozenset({"admin"}),
        groups=frozenset(),
        trace_id="t",
    )


def _consumer() -> RequestContext:
    return RequestContext(
        subject="https://idp/realms/fdp#bob",
        roles=frozenset(),
        groups=frozenset(),
        trace_id="t",
    )


def _seeded() -> _FakeService:
    svc = _FakeService(schemas={f"{DCAT}Catalog", f"{DCAT}Ontology"})
    svc.records = {"repository": ROOT, "catalog": CATALOG}
    return svc


# --- reads -----------------------------------------------------------------


@pytest.mark.unit
def test_list_returns_catalog_publicly() -> None:
    client = _build(_seeded(), ctx=_consumer())
    resp = client.get("/resource-definitions")
    assert resp.status_code == 200
    slugs = {d["slug"] for d in resp.json()["definitions"]}
    assert slugs == {"repository", "catalog"}


@pytest.mark.unit
def test_get_one_by_slug() -> None:
    client = _build(_seeded(), ctx=_consumer())
    resp = client.get("/resource-definitions/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["urlPrefix"] == "catalog"
    assert body["schema"] == f"{DCAT}Catalog"


@pytest.mark.unit
def test_get_unknown_slug_is_404() -> None:
    client = _build(_seeded(), ctx=_consumer())
    assert client.get("/resource-definitions/nope").status_code == 404


def _build_with_base(service: _FakeService, *, base_url: str) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    service.app = app
    app.state.resource_definitions = service.cache()
    app.include_router(
        build_resource_definition_router(
            service=service,  # type: ignore[arg-type]
            cache_provider=lambda: app.state.resource_definitions,
            base_url=base_url,
        )
    )
    from fdpneo_server.identity.deps import current_context

    app.dependency_overrides[current_context] = lambda: _consumer()
    return TestClient(app)


@pytest.mark.unit
def test_view_carries_absolute_links_under_a_path_base() -> None:
    # A non-root type: links are absolute, built from the serving base (ADR-0022 §4).
    client = _build_with_base(_seeded(), base_url="https://example.org/fdp")
    links = client.get("/resource-definitions/catalog").json()["links"]
    assert links["self"] == "https://example.org/fdp/fdp-api/resource-definitions/catalog"
    assert links["container"] == "https://example.org/fdp/catalog"
    assert links["spec"] == "https://example.org/fdp/catalog/spec"


@pytest.mark.unit
def test_root_view_links_collapse_to_the_base() -> None:
    client = _build_with_base(_seeded(), base_url="https://example.org/fdp")
    links = client.get("/resource-definitions/repository").json()["links"]
    # The root's container is the base itself; its spec is the root-level view.
    assert links["container"] == "https://example.org/fdp"
    assert links["spec"] == "https://example.org/fdp/spec"
    assert links["self"] == "https://example.org/fdp/fdp-api/resource-definitions/repository"


@pytest.mark.unit
def test_list_view_includes_links_for_every_definition() -> None:
    client = _build_with_base(_seeded(), base_url="https://example.org/fdp")
    for d in client.get("/resource-definitions").json()["definitions"]:
        assert set(d["links"]) == {"self", "container", "spec"}
        assert d["links"]["self"].startswith("https://example.org/fdp/")


# --- create ----------------------------------------------------------------


@pytest.mark.unit
def test_create_requires_admin() -> None:
    client = _build(_seeded(), ctx=_consumer())
    resp = client.post(
        "/resource-definitions",
        json={"urlPrefix": "ontology", "name": "Ontology", "schema": f"{DCAT}Ontology"},
    )
    assert resp.status_code == 403


@pytest.mark.unit
def test_create_new_type_lights_up_in_catalog() -> None:
    svc = _seeded()
    client = _build(svc, ctx=_admin())
    resp = client.post(
        "/resource-definitions",
        json={"urlPrefix": "ontology", "name": "Ontology", "schema": f"{DCAT}Ontology"},
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "ontology"
    # Reflected in a subsequent public read (cache was republished).
    listed = client.get("/resource-definitions").json()["definitions"]
    assert any(d["slug"] == "ontology" for d in listed)


@pytest.mark.unit
def test_create_rejects_duplicate_slug() -> None:
    client = _build(_seeded(), ctx=_admin())
    resp = client.post(
        "/resource-definitions",
        json={"urlPrefix": "catalog", "name": "Catalog", "schema": f"{DCAT}Catalog"},
    )
    assert resp.status_code == 409


@pytest.mark.unit
def test_create_rejects_reserved_prefix() -> None:
    # ``fdp-api`` is the single reserved first path segment — every fixed FDP
    # endpoint lives under it, so all other root words (sparql, schemas, …) are
    # now free for user-defined types.
    svc = _FakeService(schemas={f"{DCAT}Catalog"})
    svc.records = {"repository": ROOT}
    client = _build(svc, ctx=_admin())
    resp = client.post(
        "/resource-definitions",
        json={"urlPrefix": "fdp-api", "name": "Fdp Api", "schema": f"{DCAT}Catalog"},
    )
    assert resp.status_code == 400


@pytest.mark.unit
def test_create_rejects_unknown_schema() -> None:
    svc = _seeded()
    client = _build(svc, ctx=_admin())
    resp = client.post(
        "/resource-definitions",
        json={"urlPrefix": "ontology", "name": "Ontology", "schema": "urn:not-published"},
    )
    assert resp.status_code == 400
    assert svc.put_calls == []  # never reached the service


# --- replace (add child link to existing type) -----------------------------


@pytest.mark.unit
def test_replace_adds_child_link_to_existing_type() -> None:
    svc = _seeded()
    client = _build(svc, ctx=_admin())
    # Catalog now also contains Ontology metadata.
    resp = client.put(
        "/resource-definitions/catalog",
        json={
            "urlPrefix": "catalog",
            "name": "Catalog",
            "schema": f"{DCAT}Catalog",
            "children": [
                {"relationUri": f"{DCAT}dataset", "target": "ontology", "title": "Ontologies"}
            ],
        },
    )
    assert resp.status_code == 200
    (child,) = resp.json()["children"]
    assert child["target"] == "ontology"


@pytest.mark.unit
def test_replace_rejects_slug_change() -> None:
    client = _build(_seeded(), ctx=_admin())
    resp = client.put(
        "/resource-definitions/catalog",
        json={"urlPrefix": "renamed", "name": "Renamed", "schema": f"{DCAT}Catalog"},
    )
    assert resp.status_code == 400


# --- delete ----------------------------------------------------------------


@pytest.mark.unit
def test_delete_removes_type() -> None:
    svc = _seeded()
    client = _build(svc, ctx=_admin())
    assert client.delete("/resource-definitions/catalog").status_code == 204
    assert "catalog" not in svc.records


@pytest.mark.unit
def test_delete_root_is_rejected() -> None:
    client = _build(_seeded(), ctx=_admin())
    assert client.delete("/resource-definitions/repository").status_code == 400


@pytest.mark.unit
def test_delete_requires_admin() -> None:
    client = _build(_seeded(), ctx=_consumer())
    assert client.delete("/resource-definitions/catalog").status_code == 403
