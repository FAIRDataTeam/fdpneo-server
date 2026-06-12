"""Unit tests for the modular-profile reconciliation (task 15.2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import HttpUrl, PostgresDsn
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.config import OIDCSettings, Settings, TripleStoreSettings
from fdp.metadata.profiles import load_profile
from fdp.metadata.profiles.migrate_modular import migrate_to_modular_profile
from fdp.metadata.states import MetadataState
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import DCT, FDP_RESOURCE_DEFINITION, LDP

BASE = "http://localhost:8000"
DCAT = "http://www.w3.org/ns/dcat#"
NEW_ROOT_CLASS = URIRef(DCAT + "Catalog")
OLD_ROOT_CLASS = URIRef("https://w3id.org/fdp/o#Repository")
NEW_REL = URIRef(DCAT + "dataset")
OLD_REL = URIRef(DCAT + "catalog")
RIGHTS = URIRef(f"{BASE}/policies/system-default")

# A modular-style bundle: root re-typed to dcat:Catalog with a dcat:dataset child.
MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: modular-test
  version: 0.2.0
schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl
  - id: dcat:Dataset
    path: schemas/dataset.ttl
offers:
  - id: system-default
    path: offers/system-default.ttl
    isSystemDefault: true
resourceDefinitions:
  - urlPrefix: ""
    name: FAIRDataPoint
    schema: dcat:Catalog
    children:
      - relationUri: dcat:dataset
        target: dataset
        title: Datasets
  - urlPrefix: dataset
    name: Dataset
    schema: dcat:Dataset
"""

DATASET_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
<http://www.w3.org/ns/dcat#Dataset>
    a sh:NodeShape ; sh:targetClass dcat:Dataset ;
    sh:property [ sh:path dct:title ; sh:minCount 1 ] .
"""


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
    async def get_graph(self, record_uri: str) -> Graph:
        return self.graphs.get(str(record_graph_uri(record_uri)), Graph())

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

    async def delete_graph(self, record_uri: str) -> None:
        self.graphs.pop(str(record_graph_uri(record_uri)), None)

    # --- adapter ---
    async def query(self, sparql: str, *, accept: str = "") -> bytes:
        del accept
        bindings = [
            {"rd": {"value": str(s)}}
            for g in self.graphs.values()
            for s in g.subjects(RDF.type, FDP_RESOURCE_DEFINITION)
        ]
        return json.dumps({"results": {"bindings": bindings}}).encode()


def _pre_15_2_root() -> Graph:
    """A root record as a pre-15.2 deployment left it: old class + old relation."""
    g = Graph()
    s = URIRef(BASE)
    g.add((s, RDF.type, OLD_ROOT_CLASS))
    g.add((s, RDF.type, LDP.DirectContainer))
    g.add((s, LDP.membershipResource, s))
    g.add((s, LDP.insertedContentRelation, LDP.MemberSubject))
    g.add((s, LDP.hasMemberRelation, OLD_REL))
    g.add((s, DCT.title, Literal("My FDP")))  # authored metadata — must survive
    g.add((s, DCT.rights, RIGHTS))
    return g


@pytest.fixture
def profile(write_bundle: Callable[..., Path]):
    return load_profile(
        write_bundle(
            manifest_text=MANIFEST,
            extra_files={"schemas/dataset.ttl": DATASET_SHAPE_TTL},
        )
    )


@pytest.mark.unit
async def test_migrate_retypes_root_and_writes_config(profile) -> None:
    store = _FakeStore(graphs={BASE: _pre_15_2_root()})

    report = await migrate_to_modular_profile(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=_settings(),
    )

    # Schemas + RDs written (the store had none at those IRIs).
    assert len(report.schemas_written) == 2
    assert len(report.resource_definitions_written) == 2
    assert report.root_retyped == BASE
    assert report.changed is True

    root = store.graphs[BASE]
    s = URIRef(BASE)
    # Re-typed to the new class; old domain type gone.
    assert (s, RDF.type, NEW_ROOT_CLASS) in root
    assert (s, RDF.type, OLD_ROOT_CLASS) not in root
    # Membership reset to the new RD's relation; stale relation pruned.
    assert (s, LDP.hasMemberRelation, NEW_REL) in root
    assert (s, LDP.hasMemberRelation, OLD_REL) not in root
    assert (s, RDF.type, LDP.DirectContainer) in root
    assert (s, LDP.membershipResource, s) in root
    # Authored metadata preserved.
    assert (s, DCT.title, Literal("My FDP")) in root
    assert (s, DCT.rights, RIGHTS) in root


@pytest.mark.unit
async def test_migrate_is_idempotent(profile) -> None:
    store = _FakeStore(graphs={BASE: _pre_15_2_root()})
    settings = _settings()

    first = await migrate_to_modular_profile(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=settings,
    )
    assert first.changed is True

    second = await migrate_to_modular_profile(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=settings,
    )
    assert second.changed is False
    assert second.schemas_written == []
    assert second.resource_definitions_written == []
    assert second.root_retyped is None
    assert second.root_already_current is True


@pytest.mark.unit
async def test_migrate_no_root_record_is_safe(profile) -> None:
    """A store without a root record: config is written, root step is a no-op."""
    store = _FakeStore()

    report = await migrate_to_modular_profile(
        profile,
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert report.root_retyped is None
    assert report.root_already_current is False
    assert len(report.schemas_written) == 2
