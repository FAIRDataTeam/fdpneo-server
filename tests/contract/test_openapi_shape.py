"""OpenAPI contract test (operational-readiness plan, item #12).

Pins the *shape* of the OpenAPI document the client (``fdp-client``)
generates types from. Adding/removing client-facing routes — or
changing operation IDs in the dynamic surface — must trip this test
so the team has an explicit prompt to coordinate a matching client
PR (HANDOFF.md "Coordination with the client repo").

Two states are covered:

1. **Static surface** — what every deployment exposes regardless of
   the applied profile: ``/healthz``, the dashboard
   (``/metrics/*``), the data provider (``/data/*``), the SPARQL
   endpoint (``/sparql``), and the LDP catch-all on the root.
2. **Dynamic surface** — what auto-bootstrap adds: per-type paths
   derived from the resource-definition cache, plus their
   ``Metadata: <Name>`` tags.

When this test fails, the diff in the test body documents what the
client expects; pair the change with an ``fdp-client`` PR before
merging.
"""

from __future__ import annotations

from typing import Any

import pytest

from fdp.main import create_app
from fdp.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
)
from fdp.shared.reserved import RESERVED_API_PATH


def _spec_without_cache() -> dict[str, Any]:
    app = create_app()
    assert app.state.resource_definitions is None
    return app.openapi()


def _spec_with_cache(cache: ResourceDefinitionCache) -> dict[str, Any]:
    app = create_app()
    app.state.resource_definitions = cache
    app.openapi_schema = None  # force rebuild
    return app.openapi()


def _dcat_cache() -> ResourceDefinitionCache:
    return ResourceDefinitionCache(
        [
            ResourceDefinition(
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


# --- static surface --------------------------------------------------------


@pytest.mark.unit
def test_openapi_document_advertises_supported_version() -> None:
    spec = _spec_without_cache()
    assert spec.get("openapi", "").startswith("3.")
    assert spec.get("info", {}).get("title") == "FAIR Data Point"


@pytest.mark.unit
def test_healthz_is_documented_with_get() -> None:
    spec = _spec_without_cache()
    assert "/fdp-api/healthz" in spec["paths"]
    assert "get" in spec["paths"]["/fdp-api/healthz"]


@pytest.mark.unit
def test_metrics_dashboard_paths_are_documented() -> None:
    """Every dashboard endpoint the client renders against."""
    spec = _spec_without_cache()
    expected = {
        "/fdp-api/metrics/summary",
        "/fdp-api/metrics/timeseries/daily",
        "/fdp-api/metrics/top-resources",
        "/fdp-api/metrics/geography",
    }
    missing = expected - set(spec["paths"].keys())
    assert not missing, f"dashboard routes missing: {sorted(missing)}"


@pytest.mark.unit
def test_data_provider_paths_are_documented() -> None:
    spec = _spec_without_cache()
    paths = spec["paths"]
    distribution_paths = {p for p in paths if p.startswith("/fdp-api/data/")}
    # GET /data/{distribution_id} and GET/POST /data/{distribution_id}/sparql
    assert any(p.endswith("/{distribution_id}") for p in distribution_paths), (
        f"data download path missing; saw {distribution_paths}"
    )
    assert any(p.endswith("/{distribution_id}/sparql") for p in distribution_paths), (
        f"per-distribution SPARQL path missing; saw {distribution_paths}"
    )


@pytest.mark.unit
def test_sparql_endpoint_supports_get_and_post() -> None:
    spec = _spec_without_cache()
    assert "/fdp-api/sparql" in spec["paths"], list(spec["paths"].keys())[:20]
    methods = spec["paths"]["/fdp-api/sparql"]
    assert "get" in methods
    assert "post" in methods


@pytest.mark.unit
def test_ldp_catch_all_is_present() -> None:
    """The LDP catch-all serves seven verbs at /{path:path}.

    FastAPI surfaces parametric paths with ``{name}`` (without the
    ``:path`` converter suffix) in the OpenAPI document. The exact
    name (``path``) is part of the API surface — the client uses it
    when constructing dynamic record URLs.
    """
    spec = _spec_without_cache()
    catch_all = "/{path}"
    assert catch_all in spec["paths"], (
        f"LDP catch-all missing; got: {[p for p in spec['paths'] if '{' in p]}"
    )
    methods = spec["paths"][catch_all]
    for verb in ("get", "head", "put", "post", "patch", "delete", "options"):
        assert verb in methods, f"LDP catch-all missing {verb.upper()}"


# --- dynamic surface -------------------------------------------------------


@pytest.mark.unit
def test_dynamic_paths_appear_only_after_cache_install() -> None:
    """A fresh deployment without a cache shows no typed paths."""
    spec = _spec_without_cache()
    fdp_paths = [
        p
        for p, item in spec["paths"].items()
        if isinstance(item, dict) and "x-fdp-resource-definition" in item
    ]
    assert fdp_paths == []


@pytest.mark.unit
def test_dynamic_per_type_paths_are_documented_after_cache_install() -> None:
    spec = _spec_with_cache(_dcat_cache())
    expected_paths = {
        "/",  # Repository CRUD (root)
        "/fdp-api/spec",  # Repository SHACL shape
        "/fdp-api/expanded",  # Repository with parents
        "/fdp-api/page/{childPrefix}",  # Repository children listing
        "/catalog",  # Catalog collection (POST)
        "/catalog/{id}",  # Catalog CRUD
        "/fdp-api/catalog/{id}/spec",  # Catalog SHACL shape
        "/fdp-api/catalog/{id}/expanded",  # Catalog with parents
    }
    missing = expected_paths - set(spec["paths"].keys())
    assert not missing, f"typed paths missing after cache install: {sorted(missing)}"


@pytest.mark.unit
def test_dynamic_operation_ids_are_stable_and_per_type() -> None:
    """Operation IDs are the contract surface the client codegen uses."""
    spec = _spec_with_cache(_dcat_cache())

    # Root Repository
    assert spec["paths"]["/"]["get"]["operationId"] == "getRepository"
    assert spec["paths"]["/"]["put"]["operationId"] == "replaceRepository"
    assert spec["paths"]["/"]["delete"]["operationId"] == "deleteRepository"
    # Repository has no POST (only members can be created on collections).
    assert "post" not in spec["paths"]["/"]

    # Catalog collection + member
    assert spec["paths"]["/catalog"]["post"]["operationId"] == "createCatalog"
    assert spec["paths"]["/catalog/{id}"]["get"]["operationId"] == "getCatalog"
    assert spec["paths"]["/catalog/{id}"]["put"]["operationId"] == "replaceCatalog"
    assert spec["paths"]["/catalog/{id}"]["delete"]["operationId"] == "deleteCatalog"


@pytest.mark.unit
def test_dynamic_tags_pair_with_paths() -> None:
    spec = _spec_with_cache(_dcat_cache())
    tag_names = {t["name"] for t in spec.get("tags", [])}
    assert "Metadata: Repository" in tag_names
    assert "Metadata: Catalog" in tag_names


@pytest.mark.unit
def test_dynamic_paths_carry_fdp_extension() -> None:
    """The extension drives idempotent re-injection on profile re-apply."""
    spec = _spec_with_cache(_dcat_cache())
    for path, item in spec["paths"].items():
        if path in {"/", "/fdp-api/spec", "/fdp-api/expanded", "/fdp-api/page/{childPrefix}"}:
            assert item.get("x-fdp-resource-definition") == "(root)"
        elif path.startswith("/catalog"):
            assert item.get("x-fdp-resource-definition") == "catalog"


# --- regression / sentinel -------------------------------------------------


@pytest.mark.unit
def test_no_undocumented_internal_routes_leak_into_spec() -> None:
    """Guard the reserved-segment invariant: nothing leaks at the root.

    Every fixed, FDP-specific endpoint must live under the reserved API
    segment (``/fdp-api``) so the root namespace is free for user-defined
    resource types. Without a resource-definition cache installed, the only
    root-level path in the static surface is therefore the LDP catch-all
    (``/{path}``); every other documented path must be under ``/fdp-api``.
    A new fixed route mounted at the root would trip this — the prompt to
    move it under the reserved segment and coordinate an `fdp-client` PR.
    """
    spec = _spec_without_cache()
    surfaced = set(spec["paths"])
    leaked_at_root = {
        p for p in surfaced if p != "/{path}" and not p.startswith(f"{RESERVED_API_PATH}/")
    }
    assert not leaked_at_root, (
        f"routes outside the reserved API segment: {sorted(leaked_at_root)} — "
        "fixed endpoints must live under /fdp-api so they cannot clash with "
        "user-defined resource types."
    )
