"""Unit tests for :mod:`fdp.metadata.profiles.iri` slug/IRI derivation (task 10.5)."""

from __future__ import annotations

import pytest
from pydantic import HttpUrl, PostgresDsn

from fdp.config import OIDCSettings, Settings, TripleStoreSettings
from fdp.metadata.profiles.iri import IRIExpander, schema_slug

BASE = "http://localhost:8000"


def _expander() -> IRIExpander:
    return IRIExpander(
        settings=Settings(
            base_url=HttpUrl(BASE),
            postgres_dsn=PostgresDsn("postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test"),
            triplestore=TripleStoreSettings(
                query_endpoint=HttpUrl("http://triplestore.local/query"),
                update_endpoint=HttpUrl("http://triplestore.local/update"),
            ),
            oidc=OIDCSettings(issuer=HttpUrl("http://idp.local/realms/fdp"), audience="fdp"),
        )
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("fdp:Repository", "repository"),
        ("dcat:Catalog", "catalog"),
        ("dcat:DataService", "data-service"),  # camelCase → kebab
        ("dcat:Distribution", "distribution"),
        ("http://www.w3.org/ns/dcat#Catalog", "catalog"),  # absolute, fragment
        ("https://w3id.org/fdp/o#Repository", "repository"),
        ("http://example.org/schema/MyType", "my-type"),  # absolute, path
    ],
)
def test_schema_slug(identifier: str, expected: str) -> None:
    assert schema_slug(identifier) == expected


@pytest.mark.unit
def test_schema_storage_iri_lands_in_schemas_namespace() -> None:
    exp = _expander()
    assert exp.schema_storage_iri("dcat:Catalog") == f"{BASE}/fdp-api/schemas/catalog"
    assert exp.schema_storage_iri("dcat:DataService") == f"{BASE}/fdp-api/schemas/data-service"


@pytest.mark.unit
def test_schema_storage_iri_is_distinct_from_class_iri() -> None:
    exp = _expander()
    # The vocabulary/class IRI (schema_iri) and the storage IRI must differ —
    # that distinction is the whole point of the migration.
    assert exp.schema_iri("dcat:Catalog") == "http://www.w3.org/ns/dcat#Catalog"
    assert exp.schema_storage_iri("dcat:Catalog") != exp.schema_iri("dcat:Catalog")
