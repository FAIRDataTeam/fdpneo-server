"""Unit tests for ``fdp.metadata.graphs``."""

from __future__ import annotations

import pytest
from rdflib import URIRef

from fdp.metadata.graphs import (
    audit_graph_uri,
    data_graph_uri,
    is_audit_graph_uri,
    is_internal_graph_uri,
    is_meta_graph_uri,
    is_resource_definition_graph_uri,
    meta_graph_uri,
    record_graph_uri,
    record_uri_from_sibling,
    resource_definition_graph_uri,
    state_record_iri,
)
from fdp.shared.graphs import (
    is_profile_graph_uri,
    profile_graph_uri,
    profile_version_graph_uri,
    schema_graph_uri,
    schema_version_graph_uri,
)

RECORD = "https://example.org/records/abc"
RECORD_URI = URIRef(RECORD)


@pytest.mark.unit
def test_record_graph_uri_returns_the_record_uri() -> None:
    assert record_graph_uri(RECORD) == RECORD_URI
    assert record_graph_uri(RECORD_URI) == RECORD_URI


@pytest.mark.unit
def test_trailing_slash_is_normalised_away() -> None:
    assert record_graph_uri(RECORD + "/") == RECORD_URI
    assert meta_graph_uri(RECORD + "/") == URIRef(RECORD + "/meta")


@pytest.mark.unit
def test_meta_and_audit_siblings_have_canonical_suffixes() -> None:
    assert meta_graph_uri(RECORD) == URIRef(RECORD + "/meta")
    assert audit_graph_uri(RECORD) == URIRef(RECORD + "/audit")


@pytest.mark.unit
def test_predicates_classify_correctly() -> None:
    assert is_meta_graph_uri(meta_graph_uri(RECORD))
    assert is_audit_graph_uri(audit_graph_uri(RECORD))
    assert not is_meta_graph_uri(RECORD_URI)
    assert not is_audit_graph_uri(RECORD_URI)


@pytest.mark.unit
def test_record_uri_from_sibling_round_trip() -> None:
    assert record_uri_from_sibling(meta_graph_uri(RECORD)) == RECORD_URI
    assert record_uri_from_sibling(audit_graph_uri(RECORD)) == RECORD_URI
    assert record_uri_from_sibling(RECORD_URI) is None


BASE = "http://localhost:8000"


@pytest.mark.unit
def test_resource_definition_graph_uri() -> None:
    assert resource_definition_graph_uri(BASE, "catalog") == URIRef(
        f"{BASE}/fdp-api/resource-definitions/catalog"
    )
    # Trailing slash on base is normalised.
    assert resource_definition_graph_uri(BASE + "/", "catalog") == URIRef(
        f"{BASE}/fdp-api/resource-definitions/catalog"
    )


@pytest.mark.unit
def test_is_resource_definition_graph_uri() -> None:
    rd = resource_definition_graph_uri(BASE, "catalog")
    assert is_resource_definition_graph_uri(rd)
    assert is_resource_definition_graph_uri(meta_graph_uri(rd))  # its meta sibling too
    assert not is_resource_definition_graph_uri(f"{BASE}/catalog/c-1")


@pytest.mark.unit
def test_state_record_iri_root_vs_managed() -> None:
    # User-defined LDP records live at the root: the path maps straight through.
    assert state_record_iri(BASE, "catalog/c-1") == URIRef(f"{BASE}/catalog/c-1")
    # Server-managed resources live under the reserved prefix, which the state
    # router strips from the request path — state_record_iri re-adds it so the
    # transition targets the stored graph (regression: policies/licenses 404'd).
    assert state_record_iri(BASE, "policies/p1") == URIRef(f"{BASE}/fdp-api/policies/p1")
    assert state_record_iri(BASE, "licenses/l1") == URIRef(f"{BASE}/fdp-api/licenses/l1")
    assert state_record_iri(BASE, "schemas/s1") == URIRef(f"{BASE}/fdp-api/schemas/s1")
    assert state_record_iri(BASE, "profiles/dataset") == URIRef(f"{BASE}/fdp-api/profiles/dataset")
    assert state_record_iri(f"{BASE}/", "resource-definitions/catalog") == URIRef(
        f"{BASE}/fdp-api/resource-definitions/catalog"
    )
    # A record whose first path segment merely resembles a managed name is not
    # treated as managed (exact leaf-segment match only).
    assert state_record_iri(BASE, "policies-archive/x") == URIRef(f"{BASE}/policies-archive/x")


@pytest.mark.unit
def test_is_internal_graph_uri_covers_all_machinery_classes() -> None:
    rd = resource_definition_graph_uri(BASE, "catalog")
    # Internal: meta, audit, resource-definition records (and their siblings).
    assert is_internal_graph_uri(meta_graph_uri(RECORD))
    assert is_internal_graph_uri(audit_graph_uri(RECORD))
    assert is_internal_graph_uri(rd)
    assert is_internal_graph_uri(meta_graph_uri(rd))
    # Not internal: ordinary record graphs and distribution data graphs.
    assert not is_internal_graph_uri(RECORD_URI)
    assert not is_internal_graph_uri(f"{BASE}/catalog/c-1")
    assert not is_internal_graph_uri(data_graph_uri(RECORD))
    # Profiles and schemas are public reference documents, NOT internal (ADR-0019).
    assert not is_internal_graph_uri(profile_graph_uri(BASE, "dataset"))


@pytest.mark.unit
def test_profile_and_versioned_graph_uris() -> None:
    # Stable profile IRI — the target of a record's dct:conformsTo.
    assert profile_graph_uri(BASE, "dataset") == URIRef(f"{BASE}/fdp-api/profiles/dataset")
    assert profile_graph_uri(f"{BASE}/", "dataset") == URIRef(f"{BASE}/fdp-api/profiles/dataset")
    # Immutable versioned snapshots build on the stable IRI (single scheme).
    assert profile_version_graph_uri(BASE, "dataset", "1.2.0") == URIRef(
        f"{BASE}/fdp-api/profiles/dataset/1.2.0"
    )
    assert schema_version_graph_uri(BASE, "dataset", "1.2.0") == URIRef(
        f"{schema_graph_uri(BASE, 'dataset')}/1.2.0"
    )


@pytest.mark.unit
def test_is_profile_graph_uri() -> None:
    profile = profile_graph_uri(BASE, "dataset")
    assert is_profile_graph_uri(profile)
    assert is_profile_graph_uri(profile_version_graph_uri(BASE, "dataset", "1.2.0"))
    assert is_profile_graph_uri(meta_graph_uri(profile))  # its meta sibling too
    assert not is_profile_graph_uri(schema_graph_uri(BASE, "dataset"))
    assert not is_profile_graph_uri(f"{BASE}/catalog/c-1")
