"""Unit tests for :mod:`fdp.metadata.openapi` (sub-task 15d).

Covers the per-RD path set, root vs non-root differences, the FDP
extension that lets us find and remove paths on profile change,
idempotence of repeated injection, and tag generation.
"""

from __future__ import annotations

from typing import Any

import pytest

from fdp.metadata.openapi import inject_resource_definition_paths
from fdp.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
)


# --- fixtures -------------------------------------------------------------


def _cache(*items: ResourceDefinition) -> ResourceDefinitionCache:
    return ResourceDefinitionCache(items, base_url="http://localhost:8000")


_REPO_RD = ResourceDefinition(
    url_prefix="",
    name="Repository",
    schema_iri="https://w3id.org/fdp/o#Repository",
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
)

_CATALOG_RD = ResourceDefinition(
    url_prefix="catalog",
    name="Catalog",
    schema_iri="http://www.w3.org/ns/dcat#Catalog",
    children=(
        ChildLinkInfo(
            relation_uri="http://www.w3.org/ns/dcat#dataset",
            target_prefix="dataset",
            target_name="Dataset",
            target_schema_iri="http://www.w3.org/ns/dcat#Dataset",
            title="Datasets",
            tags_uri=None,
        ),
    ),
)

_DATASET_RD = ResourceDefinition(
    url_prefix="dataset",
    name="Dataset",
    schema_iri="http://www.w3.org/ns/dcat#Dataset",
    children=(),
)


def _empty_spec() -> dict[str, Any]:
    return {"openapi": "3.1.0", "info": {"title": "FDP", "version": "0"}}


# --- root resource definition ---------------------------------------------


@pytest.mark.unit
def test_root_emits_crud_at_slash_with_no_post() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD))
    root = spec["paths"]["/"]
    assert set(root.keys()) >= {"get", "put", "delete", "x-fdp-resource-definition"}
    assert "post" not in root


@pytest.mark.unit
def test_root_emits_spec_expanded_and_page_paths() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD))
    assert "/spec" in spec["paths"]
    assert "/expanded" in spec["paths"]
    assert "/page/{childPrefix}" in spec["paths"]


@pytest.mark.unit
def test_root_without_children_omits_page_path() -> None:
    childless = ResourceDefinition(
        url_prefix="", name="Repository",
        schema_iri="http://x", children=(),
    )
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(childless))
    assert "/page/{childPrefix}" not in spec["paths"]


# --- non-root resource definition -----------------------------------------


@pytest.mark.unit
def test_non_root_emits_collection_post() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    collection = spec["paths"]["/catalog"]
    assert "post" in collection
    assert collection["x-fdp-resource-definition"] == "catalog"


@pytest.mark.unit
def test_non_root_emits_member_crud_under_id_param() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    member = spec["paths"]["/catalog/{id}"]
    assert set(member.keys()) >= {"get", "put", "delete"}
    # The id param is declared on each operation.
    for op_name in ("get", "put", "delete"):
        params = member[op_name]["parameters"]
        assert any(p["name"] == "id" and p["in"] == "path" for p in params)


@pytest.mark.unit
def test_non_root_emits_spec_expanded_page_paths_with_id() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    paths = spec["paths"]
    assert "/catalog/{id}/spec" in paths
    assert "/catalog/{id}/expanded" in paths
    assert "/catalog/{id}/page/{childPrefix}" in paths


@pytest.mark.unit
def test_leaf_type_without_children_has_no_page_path() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _DATASET_RD))
    assert "/dataset/{id}/page/{childPrefix}" not in spec["paths"]


# --- tags ------------------------------------------------------------------


@pytest.mark.unit
def test_one_tag_per_resource_definition() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD, _DATASET_RD))
    tag_names = [t["name"] for t in spec["tags"]]
    assert tag_names == [
        "Metadata: Repository",
        "Metadata: Catalog",
        "Metadata: Dataset",
    ]


@pytest.mark.unit
def test_every_operation_carries_the_corresponding_tag() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    paths = spec["paths"]
    # Pick one operation from each RD.
    assert "Metadata: Repository" in paths["/"]["get"]["tags"]
    assert "Metadata: Catalog" in paths["/catalog"]["post"]["tags"]
    assert "Metadata: Catalog" in paths["/catalog/{id}"]["get"]["tags"]


# --- FDP extension --------------------------------------------------------


@pytest.mark.unit
def test_every_emitted_path_item_carries_fdp_extension() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    for path, item in spec["paths"].items():
        assert "x-fdp-resource-definition" in item, path


@pytest.mark.unit
def test_root_extension_uses_root_sentinel() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD))
    assert spec["paths"]["/"]["x-fdp-resource-definition"] == "(root)"


# --- request / response bodies --------------------------------------------


@pytest.mark.unit
def test_put_request_body_lists_all_rdf_media_types() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    content = spec["paths"]["/catalog/{id}"]["put"]["requestBody"]["content"]
    assert set(content.keys()) == {
        "text/turtle",
        "application/ld+json",
        "application/rdf+xml",
        "application/n-triples",
    }


@pytest.mark.unit
def test_get_responses_include_2xx_and_error_classes() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    responses = spec["paths"]["/catalog/{id}"]["get"]["responses"]
    assert set(responses.keys()) >= {"200", "400", "401", "403", "404"}


@pytest.mark.unit
def test_post_returns_201() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    responses = spec["paths"]["/catalog"]["post"]["responses"]
    assert "201" in responses
    assert "200" not in responses


@pytest.mark.unit
def test_delete_returns_204_not_200() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    responses = spec["paths"]["/catalog/{id}"]["delete"]["responses"]
    assert "204" in responses
    assert "200" not in responses


# --- idempotence / re-injection -------------------------------------------


@pytest.mark.unit
def test_re_injection_replaces_old_fdp_paths() -> None:
    """A second call after the profile changes drops the old paths."""
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    assert "/catalog" in spec["paths"]
    # Re-inject with only the Repository — Catalog routes must disappear.
    inject_resource_definition_paths(spec, _cache(_REPO_RD))
    assert "/catalog" not in spec["paths"]
    assert "/catalog/{id}" not in spec["paths"]
    assert "/" in spec["paths"]  # the root remains


@pytest.mark.unit
def test_re_injection_keeps_non_fdp_paths() -> None:
    """Static FastAPI paths (no FDP extension) survive re-injection."""
    spec = _empty_spec()
    spec.setdefault("paths", {})["/healthz"] = {
        "get": {"summary": "Liveness", "responses": {"200": {"description": "OK"}}}
    }
    inject_resource_definition_paths(spec, _cache(_REPO_RD))
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    assert "/healthz" in spec["paths"]


@pytest.mark.unit
def test_re_injection_replaces_fdp_tags_only() -> None:
    spec = _empty_spec()
    spec["tags"] = [{"name": "Internal", "x-priority": 0}]
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD))
    inject_resource_definition_paths(spec, _cache(_REPO_RD))
    names = [t["name"] for t in spec["tags"]]
    assert "Internal" in names
    assert "Metadata: Catalog" not in names
    assert "Metadata: Repository" in names


# --- operation ids are stable / unique ------------------------------------


@pytest.mark.unit
def test_operation_ids_are_unique_across_emitted_paths() -> None:
    spec = _empty_spec()
    inject_resource_definition_paths(spec, _cache(_REPO_RD, _CATALOG_RD, _DATASET_RD))
    op_ids: list[str] = []
    for item in spec["paths"].values():
        for key, op in item.items():
            if key.startswith("x-"):
                continue
            op_ids.append(op["operationId"])
    assert len(op_ids) == len(set(op_ids)), f"duplicates: {op_ids}"
