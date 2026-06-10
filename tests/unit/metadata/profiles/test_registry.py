"""Unit tests for :mod:`fdp.metadata.profiles.registry` (sub-task 15b)."""

from __future__ import annotations

import pytest
from pydantic import HttpUrl, PostgresDsn

from fdp.config import OIDCSettings, Settings, TripleStoreSettings
from fdp.metadata.profiles.iri import IRIExpander
from fdp.metadata.profiles.manifest import (
    ChildLink,
    ResourceDefinitionEntry,
)
from fdp.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
    build_cache_from_manifest,
)

BASE = "http://localhost:8000"


def _settings() -> Settings:
    return Settings(
        postgres_dsn=PostgresDsn("postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test"),
        triplestore=TripleStoreSettings(
            query_endpoint=HttpUrl("http://triplestore.local/query"),
            update_endpoint=HttpUrl("http://triplestore.local/update"),
        ),
        oidc=OIDCSettings(
            issuer=HttpUrl("http://idp.local/realms/fdp"),
            audience="fdp",
        ),
    )


def _expander() -> IRIExpander:
    return IRIExpander(settings=_settings())


# Sample cache with three types: root Repository, Catalog (with Dataset child),
# Dataset (no children).
def _three_type_cache() -> ResourceDefinitionCache:
    entries = [
        ResourceDefinitionEntry(
            urlPrefix="",
            name="Repository",
            schema="fdp:Repository",
            children=[ChildLink(relationUri="dcat:catalog", target="catalog", title="Catalogs")],
        ),
        ResourceDefinitionEntry(
            urlPrefix="catalog",
            name="Catalog",
            schema="dcat:Catalog",
            children=[ChildLink(relationUri="dcat:dataset", target="dataset", title="Datasets")],
        ),
        ResourceDefinitionEntry(
            urlPrefix="dataset",
            name="Dataset",
            schema="dcat:Dataset",
        ),
    ]
    return build_cache_from_manifest(entries, expander=_expander())


# --- read accessors ------------------------------------------------------


@pytest.mark.unit
def test_all_preserves_manifest_order() -> None:
    cache = _three_type_cache()
    prefixes = [rd.url_prefix for rd in cache.all()]
    assert prefixes == ["", "catalog", "dataset"]


@pytest.mark.unit
def test_by_prefix_and_root() -> None:
    cache = _three_type_cache()
    catalog = cache.by_prefix("catalog")
    assert catalog is not None
    assert catalog.name == "Catalog"
    assert cache.by_prefix("not-a-thing") is None
    root = cache.root()
    assert root is not None
    assert root.is_root


# --- URL resolution ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url,expected_prefix",
    [
        (f"{BASE}/", ""),
        (BASE, ""),
        (f"{BASE}/catalog", "catalog"),
        (f"{BASE}/catalog/", "catalog"),
        (f"{BASE}/catalog/c-1", "catalog"),
        (f"{BASE}/catalog/c-1?foo=bar", "catalog"),
        (f"{BASE}/catalog/c-1#frag", "catalog"),
        (f"{BASE}/dataset/d-1", "dataset"),
    ],
)
def test_for_url_extracts_first_segment(url: str, expected_prefix: str) -> None:
    cache = _three_type_cache()
    rd = cache.for_url(url)
    assert rd is not None
    assert rd.url_prefix == expected_prefix


@pytest.mark.unit
def test_for_url_returns_none_for_unknown_prefix() -> None:
    cache = _three_type_cache()
    assert cache.for_url(f"{BASE}/ghost/anything") is None


@pytest.mark.unit
def test_for_url_returns_none_for_url_outside_base_url() -> None:
    cache = _three_type_cache()
    assert cache.for_url("https://elsewhere.example/catalog/c-1") is None


# --- ContainerRegistry methods --------------------------------------------


@pytest.mark.unit
def test_is_container_root_with_children() -> None:
    cache = _three_type_cache()
    assert cache.is_container(BASE) is True
    assert cache.is_container(f"{BASE}/") is True


@pytest.mark.unit
def test_is_container_collection_endpoint() -> None:
    cache = _three_type_cache()
    assert cache.is_container(f"{BASE}/catalog") is True
    assert cache.is_container(f"{BASE}/dataset") is True


@pytest.mark.unit
def test_is_container_member_resource_is_not_a_container() -> None:
    cache = _three_type_cache()
    assert cache.is_container(f"{BASE}/catalog/c-1") is False
    assert cache.is_container(f"{BASE}/dataset/d-1") is False


@pytest.mark.unit
def test_is_container_unknown_prefix_is_not_a_container() -> None:
    cache = _three_type_cache()
    assert cache.is_container(f"{BASE}/ghost") is False


@pytest.mark.unit
def test_is_container_root_without_children_is_not_a_container() -> None:
    only_root = build_cache_from_manifest(
        [
            ResourceDefinitionEntry(
                urlPrefix="",
                name="Repository",
                schema="fdp:Repository",
            )
        ],
        expander=_expander(),
    )
    assert only_root.is_container(BASE) is False


@pytest.mark.unit
def test_member_shape_for_collection() -> None:
    cache = _three_type_cache()
    # constrainedBy points at the schema's *storage* IRI (task 10.5), not the
    # vocabulary/class IRI — so a profile schema is an ordinary editable schema.
    assert cache.member_shape(f"{BASE}/catalog") == f"{BASE}/fdp-api/schemas/catalog"


@pytest.mark.unit
def test_member_shape_only_meaningful_at_one_segment() -> None:
    cache = _three_type_cache()
    assert cache.member_shape(f"{BASE}/catalog/c-1") is None
    assert cache.member_shape(BASE) is None


@pytest.mark.unit
def test_shape_for_resource() -> None:
    cache = _three_type_cache()
    assert cache.shape_for(BASE) == f"{BASE}/fdp-api/schemas/repository"
    assert cache.shape_for(f"{BASE}/catalog/c-1") == f"{BASE}/fdp-api/schemas/catalog"
    assert cache.shape_for(f"{BASE}/ghost/anything") is None


# --- build_cache_from_manifest --------------------------------------------


@pytest.mark.unit
def test_build_resolves_schema_to_storage_iri() -> None:
    cache = _three_type_cache()
    root = cache.root()
    assert root is not None
    # A schema CURIE resolves to its storage IRI under the schemas namespace
    # (task 10.5), kebab-cased from the local name.
    assert root.schema_iri == f"{BASE}/fdp-api/schemas/repository"
    catalog = cache.by_prefix("catalog")
    assert catalog is not None
    assert catalog.schema_iri == f"{BASE}/fdp-api/schemas/catalog"


@pytest.mark.unit
def test_build_resolves_child_targets_to_expanded_iris_and_names() -> None:
    cache = _three_type_cache()
    root = cache.root()
    assert root is not None
    assert len(root.children) == 1
    link = root.children[0]
    assert link.relation_uri == "http://www.w3.org/ns/dcat#catalog"
    assert link.target_prefix == "catalog"
    assert link.target_name == "Catalog"
    # The child target's schema resolves to the target type's storage IRI.
    assert link.target_schema_iri == f"{BASE}/fdp-api/schemas/catalog"
    assert link.title == "Catalogs"


@pytest.mark.unit
def test_build_passes_through_absolute_iris_for_relations() -> None:
    """A relation_uri that's already absolute is not double-expanded."""
    entries = [
        ResourceDefinitionEntry(
            urlPrefix="",
            name="Repository",
            schema="fdp:Repository",
            children=[
                ChildLink(
                    relationUri="http://example.org/custom-relation",
                    target="catalog",
                )
            ],
        ),
        ResourceDefinitionEntry(
            urlPrefix="catalog",
            name="Catalog",
            schema="dcat:Catalog",
        ),
    ]
    cache = build_cache_from_manifest(entries, expander=_expander())
    root = cache.root()
    assert root is not None
    assert root.children[0].relation_uri == "http://example.org/custom-relation"


# --- value-type sanity ----------------------------------------------------


@pytest.mark.unit
def test_resource_definition_is_root_property() -> None:
    rd = ResourceDefinition(url_prefix="", name="R", schema_iri="http://x", children=())
    assert rd.is_root is True
    rd2 = ResourceDefinition(url_prefix="x", name="X", schema_iri="http://y", children=())
    assert rd2.is_root is False


@pytest.mark.unit
def test_child_link_info_holds_all_fields() -> None:
    link = ChildLinkInfo(
        relation_uri="r",
        target_prefix="p",
        target_name="n",
        target_schema_iri="s",
        title="t",
        tags_uri=None,
    )
    assert link.target_prefix == "p"
    assert link.tags_uri is None
