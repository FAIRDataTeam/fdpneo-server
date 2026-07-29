"""Unit tests for the schema-namespace reconciliation (task 10.5)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import HttpUrl, PostgresDsn
from rdflib import Graph
from rdflib.namespace import RDF

from fdpneo_server.config import OIDCSettings, Settings, TripleStoreSettings
from fdpneo_server.metadata.profiles import load_profile
from fdpneo_server.metadata.profiles.iri import IRIExpander
from fdpneo_server.metadata.profiles.migrate import migrate_schema_namespace
from fdpneo_server.metadata.profiles.rd_records import ResourceDefinitionRecord, record_to_graph
from fdpneo_server.metadata.states import MetadataState
from fdpneo_server.shared.graphs import record_graph_uri, resource_definition_graph_uri
from fdpneo_server.shared.namespaces import FDP_RESOURCE_DEFINITION, LDP
from tests.unit.metadata.profiles.conftest import SCHEMA_TTL

BASE = "http://localhost:8000"


def _settings() -> Settings:
    return Settings(
        base_url=HttpUrl(BASE),
        postgres_dsn=PostgresDsn("postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test"),
        triplestore=TripleStoreSettings(
            query_endpoint=HttpUrl("http://triplestore.local/query"),
            update_endpoint=HttpUrl("http://triplestore.local/update"),
        ),
        oidc=OIDCSettings(issuer=HttpUrl("http://idp.local/realms/fdp"), audience="fdp"),
    )


@dataclass
class _FakeStore:
    """Backs both the repository and the adapter over one graph dict."""

    graphs: dict[str, Graph] = field(default_factory=dict)

    # --- repository ---
    async def put_graph(
        self,
        record_uri: str,
        graph: Graph,
        *,
        subject: str | None,
        initial_state: MetadataState = MetadataState.DRAFT,
    ) -> str:
        del subject, initial_state
        self.graphs[str(record_graph_uri(record_uri))] = graph
        return "etag"

    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

    async def delete_graph(self, record_uri: str) -> None:
        self.graphs.pop(str(record_graph_uri(record_uri)), None)

    # --- adapter ---
    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        if "SELECT DISTINCT ?rd" in sparql:
            bindings = [
                {"rd": {"value": str(s)}}
                for g in self.graphs.values()
                for s in g.subjects(RDF.type, FDP_RESOURCE_DEFINITION)
            ]
            return json.dumps({"results": {"bindings": bindings}}).encode()
        # CONSTRUCT against a single named graph.
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        graph = self.graphs.get(match.group(1), Graph()) if match else Graph()
        return graph.serialize(format="turtle").encode()


@dataclass
class _StubValidator:
    invalidated: list[str] = field(default_factory=list)

    def invalidate(self, iri: str) -> None:
        self.invalidated.append(iri)


def _old_layout_store(profile_schema_old_iri: str, rd_iri: str) -> _FakeStore:
    """A store as a pre-10.5 deployment left it: schema at the class IRI, RD pointing there."""
    store = _FakeStore()
    shape = Graph()
    shape.parse(data=SCHEMA_TTL, format="turtle")
    store.graphs[profile_schema_old_iri] = shape
    record = ResourceDefinitionRecord(
        url_prefix="", name="Repository", schema_iri=profile_schema_old_iri
    )
    store.graphs[rd_iri] = record_to_graph(record, rd_iri)
    return store


@pytest.mark.unit
async def test_migrate_moves_schema_and_repoints_resource_definition(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())  # schema dcat:Catalog, root RD → dcat:Catalog
    expander = IRIExpander(settings=_settings())
    old = expander.schema_iri("dcat:Catalog")  # http://www.w3.org/ns/dcat#Catalog
    new = expander.schema_storage_iri("dcat:Catalog")  # {base}/fdp-api/schemas/catalog
    rd_iri = str(resource_definition_graph_uri(BASE, "repository"))

    store = _old_layout_store(old, rd_iri)
    validator = _StubValidator()

    report = await migrate_schema_namespace(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=_settings(),
        validator=validator,  # type: ignore[arg-type]
    )

    # Schema graph relocated.
    assert new in store.graphs
    assert old not in store.graphs
    assert report.moved == [(old, new)]

    # RD repointed to the new storage IRI.
    assert report.resource_definitions_repointed == [rd_iri]
    constrained = list(store.graphs[rd_iri].objects(None, LDP.constrainedBy))
    assert [str(o) for o in constrained] == [new]

    # Validator cache invalidated for both IRIs.
    assert old in validator.invalidated
    assert new in validator.invalidated


@pytest.mark.unit
async def test_migrate_is_idempotent(write_bundle: Callable[..., Path]) -> None:
    profile = load_profile(write_bundle())
    expander = IRIExpander(settings=_settings())
    old = expander.schema_iri("dcat:Catalog")
    new = expander.schema_storage_iri("dcat:Catalog")
    rd_iri = str(resource_definition_graph_uri(BASE, "repository"))

    store = _old_layout_store(old, rd_iri)
    first = await migrate_schema_namespace(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert first.changed is True

    # Second pass finds nothing at the old IRI — a clean no-op.
    second = await migrate_schema_namespace(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert second.moved == []
    assert second.changed is False
    assert new in second.already_migrated
    assert new in store.graphs and old not in store.graphs
