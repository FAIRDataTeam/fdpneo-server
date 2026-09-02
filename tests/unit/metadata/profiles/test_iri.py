"""Unit tests for :mod:`fdpneo_server.metadata.profiles.iri` slug/IRI derivation (task 10.5)."""

from __future__ import annotations

import pytest
from pydantic import HttpUrl, PostgresDsn
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdpneo_server.config import OIDCSettings, Settings, TripleStoreSettings
from fdpneo_server.metadata.profiles.iri import IRIExpander, expand_schema_refs, schema_slug
from fdpneo_server.shared.namespaces import DCAT, SH

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
        ("https://w3id.org/fdp/fdp-o#Repository", "repository"),
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
def test_expand_schema_refs_rewrites_placeholders_not_class_iris() -> None:
    g = Graph()
    catalog = URIRef("urn:fdp-schema:catalog")
    g.add((catalog, RDF.type, SH.NodeShape))
    g.add((catalog, SH.targetClass, DCAT.Catalog))  # real class IRI — must stay
    g.add((catalog, SH.node, URIRef("urn:fdp-schema:dataset")))  # placeholder — rewrite

    out = expand_schema_refs(g, BASE)
    cat_iri = URIRef(f"{BASE}/fdp-api/schemas/catalog")
    # Subject + sh:node placeholders → storage IRIs.
    assert (cat_iri, RDF.type, SH.NodeShape) in out
    assert (cat_iri, SH.node, URIRef(f"{BASE}/fdp-api/schemas/dataset")) in out
    # The real vocabulary IRI in sh:targetClass is untouched.
    assert (cat_iri, SH.targetClass, DCAT.Catalog) in out
    # Nothing placeholder-shaped survives.
    assert not any("urn:fdp-schema:" in str(term) for triple in out for term in triple)


@pytest.mark.unit
def test_schema_storage_iri_is_distinct_from_class_iri() -> None:
    exp = _expander()
    # The vocabulary/class IRI (schema_iri) and the storage IRI must differ —
    # that distinction is the whole point of the migration.
    assert exp.schema_iri("dcat:Catalog") == "http://www.w3.org/ns/dcat#Catalog"
    assert exp.schema_storage_iri("dcat:Catalog") != exp.schema_iri("dcat:Catalog")
