"""Unit tests for the dynamic OpenAPI wiring in :mod:`fdpneo_server.main` (sub-task 15e).

These tests bypass the lifespan (which would need a real triple store)
by constructing the app directly and manipulating
``app.state.resource_definitions`` to mirror what auto-bootstrap would do.
"""

from __future__ import annotations

import pytest

from fdpneo_server.main import _DynamicContainerRegistry, create_app
from fdpneo_server.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
)


def _two_type_cache() -> ResourceDefinitionCache:
    return ResourceDefinitionCache(
        [
            ResourceDefinition(
                url_prefix="",
                name="Repository",
                schema_iri="https://w3id.org/fdp/fdp-o#Repository",
                children=(
                    ChildLinkInfo(
                        relation_uri="http://www.w3.org/ns/dcat#catalog",
                        target_prefix="catalog",
                        target_name="Catalog",
                        target_schema_iri="http://www.w3.org/ns/dcat#Catalog",
                        title="Catalogs",
                        tags_uri=None,
                    ),
                ),
            ),
            ResourceDefinition(
                url_prefix="catalog",
                name="Catalog",
                schema_iri="http://www.w3.org/ns/dcat#Catalog",
                children=(),
            ),
        ],
        base_url="http://localhost:8000",
    )


# --- OpenAPI override ----------------------------------------------------


@pytest.mark.unit
def test_openapi_without_cache_emits_only_static_paths() -> None:
    """A fresh app with no cache returns the FastAPI-derived base spec."""
    app = create_app()
    spec = app.openapi()
    assert app.state.resource_definitions is None
    # FastAPI generates a path for /healthz from the decorator.
    assert "/fdp-api/healthz" in spec["paths"]
    # No FDP-injected paths yet.
    assert not any(
        "x-fdp-resource-definition" in v for v in spec["paths"].values() if isinstance(v, dict)
    )


@pytest.mark.unit
def test_openapi_with_cache_injects_typed_paths_and_tags() -> None:
    app = create_app()
    cache = _two_type_cache()
    app.state.resource_definitions = cache
    app.openapi_schema = None  # force rebuild
    spec = app.openapi()
    # Static paths still present.
    assert "/fdp-api/healthz" in spec["paths"]
    # Typed paths now appear.
    assert "/" in spec["paths"]  # root Repository
    assert "/catalog" in spec["paths"]  # collection POST
    assert "/catalog/{id}" in spec["paths"]
    assert "/fdp-api/catalog/{id}/spec" in spec["paths"]
    # Tag names include both per-type tags.
    tag_names = [t["name"] for t in spec.get("tags", [])]
    assert "Metadata: Repository" in tag_names
    assert "Metadata: Catalog" in tag_names


@pytest.mark.unit
def test_openapi_is_cached_between_calls_when_unchanged() -> None:
    """A second call without clearing the schema returns the same dict."""
    app = create_app()
    first = app.openapi()
    second = app.openapi()
    assert first is second


@pytest.mark.unit
def test_clearing_schema_forces_regeneration_after_cache_change() -> None:
    app = create_app()
    before = app.openapi()
    assert "/catalog" not in before["paths"]
    app.state.resource_definitions = _two_type_cache()
    app.openapi_schema = None  # mirrors what auto-bootstrap does
    after = app.openapi()
    assert "/catalog" in after["paths"]


# --- dynamic container registry ------------------------------------------


@pytest.mark.unit
def test_dynamic_registry_returns_defaults_when_cache_absent() -> None:
    app = create_app()
    registry = _DynamicContainerRegistry(app)
    assert app.state.resource_definitions is None
    assert registry.is_container("http://localhost:8000/catalog") is False
    assert registry.member_shape("http://localhost:8000/catalog") is None
    assert registry.shape_for("http://localhost:8000/catalog/c-1") is None


@pytest.mark.unit
def test_dynamic_registry_passes_through_when_cache_set() -> None:
    app = create_app()
    app.state.resource_definitions = _two_type_cache()
    registry = _DynamicContainerRegistry(app)
    # Catalog is a known prefix with no children → collection-only.
    assert registry.is_container("http://localhost:8000/catalog") is True
    assert registry.is_container("http://localhost:8000/catalog/c-1") is False
    # member_shape on /catalog → catalog's schema iri.
    assert (
        registry.member_shape("http://localhost:8000/catalog")
        == "http://www.w3.org/ns/dcat#Catalog"
    )
    # shape_for on a known prefix resolves; unknown returns None.
    assert (
        registry.shape_for("http://localhost:8000/catalog/c-1")
        == "http://www.w3.org/ns/dcat#Catalog"
    )
    assert registry.shape_for("http://localhost:8000/ghost") is None
