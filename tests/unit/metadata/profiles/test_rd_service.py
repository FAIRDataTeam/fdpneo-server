"""Unit tests for the runtime resource-definition cache rebuild + service (#3).

Covers:

* :func:`resolve_cache` — the pure cross-reference step.
* :func:`build_cache_from_repository` / :func:`load_definition_records` — store
  reads, against an in-memory fake of the triple-store adapter.
* :class:`ResourceDefinitionService` — write-through, rebuild, ``on_rebuilt``
  notification, and shape validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.metadata.profiles.rd_records import (
    ChildLinkRecord,
    ResourceDefinitionRecord,
    record_to_graph,
)
from fdp.metadata.profiles.rd_service import (
    ResourceDefinitionService,
    build_cache_from_repository,
    list_definition_iris,
)
from fdp.metadata.profiles.registry import resolve_cache
from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import FDP_RESOURCE_DEFINITION, SH

BASE = "http://localhost:8000"
DCAT = "http://www.w3.org/ns/dcat#"

CATALOG = ResourceDefinitionRecord(
    url_prefix="catalog",
    name="Catalog",
    schema_iri=f"{DCAT}Catalog",
    children=(
        ChildLinkRecord(relation_uri=f"{DCAT}dataset", target_prefix="dataset", title="Datasets"),
    ),
)
DATASET = ResourceDefinitionRecord(
    url_prefix="dataset",
    name="Dataset",
    schema_iri=f"{DCAT}Dataset",
)


def _rd_iri(slug: str) -> str:
    return f"{BASE}/resource-definitions/{slug}"


# --- resolve_cache (pure) --------------------------------------------------


@pytest.mark.unit
def test_resolve_cache_resolves_child_target_name_and_schema() -> None:
    cache = resolve_cache([CATALOG, DATASET], base_url=BASE)
    catalog = cache.by_prefix("catalog")
    assert catalog is not None
    (child,) = catalog.children
    assert child.target_prefix == "dataset"
    assert child.target_name == "Dataset"
    assert child.target_schema_iri == f"{DCAT}Dataset"


@pytest.mark.unit
def test_resolve_cache_unresolvable_target_falls_back_to_prefix() -> None:
    # Dataset definition absent → child target can't be resolved; the cache
    # falls back to the prefix as the name and an empty schema rather than
    # crashing (mirrors build_cache_from_manifest's tolerant lookup).
    cache = resolve_cache([CATALOG], base_url=BASE)
    catalog = cache.by_prefix("catalog")
    assert catalog is not None
    (child,) = catalog.children
    assert child.target_name == "dataset"
    assert child.target_schema_iri == ""


# --- fake adapter / store --------------------------------------------------


@dataclass
class _FakeStore:
    """In-memory triple store implementing the adapter + repository surface.

    Backs ``query`` (SELECT for RD IRIs, CONSTRUCT for a graph) and the
    repository ``put_graph`` / ``delete_graph`` the service calls, over one
    dict of graph-IRI → Graph so a write is visible to the next rebuild.
    """

    graphs: dict[str, Graph] = field(default_factory=dict)

    def seed(self, record: ResourceDefinitionRecord, slug: str) -> None:
        iri = _rd_iri(slug)
        self.graphs[iri] = record_to_graph(record, iri)

    # adapter surface
    async def query(self, sparql: str, *, accept: str = "", **_: object) -> bytes:
        if sparql.lstrip().startswith("SELECT"):
            bindings = [
                {"rd": {"type": "uri", "value": iri}}
                for iri, g in self.graphs.items()
                if FDP_RESOURCE_DEFINITION in set(g.objects())
            ]
            return json.dumps({"results": {"bindings": bindings}}).encode("utf-8")
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        assert match is not None
        graph = self.graphs.get(match.group(1), Graph())
        return graph.serialize(format="turtle").encode("utf-8")

    async def ask(self, sparql: str) -> bool:
        # Used by schema_exists: does the named graph hold a SHACL shape?
        match = re.search(r"GRAPH <([^>]+)>", sparql)
        assert match is not None
        graph = self.graphs.get(match.group(1), Graph())
        has_node_shape = (None, RDF.type, SH.NodeShape) in graph
        has_target_class = any(graph.triples((None, SH.targetClass, None)))
        return has_node_shape or has_target_class

    def seed_shape(self, iri: str) -> None:
        """Store a minimal SHACL shape graph at ``iri``."""
        graph = Graph()
        subject = URIRef(iri)
        graph.add((subject, RDF.type, SH.NodeShape))
        self.graphs[iri] = graph

    # repository surface
    async def put_graph(self, record_uri: str, graph: Graph, *, subject: str | None) -> str:
        del subject
        self.graphs[str(record_graph_uri(record_uri))] = graph
        return "etag"

    async def delete_graph(self, record_uri: str) -> None:
        self.graphs.pop(str(record_graph_uri(record_uri)), None)


# --- build_cache_from_repository -------------------------------------------


@pytest.mark.unit
async def test_list_definition_iris_returns_seeded_records() -> None:
    store = _FakeStore()
    store.seed(CATALOG, "catalog")
    store.seed(DATASET, "dataset")
    iris = set(await list_definition_iris(store))  # type: ignore[arg-type]
    assert iris == {_rd_iri("catalog"), _rd_iri("dataset")}


@pytest.mark.unit
async def test_build_cache_from_repository_round_trips_records() -> None:
    store = _FakeStore()
    store.seed(CATALOG, "catalog")
    store.seed(DATASET, "dataset")
    cache = await build_cache_from_repository(store, base_url=BASE)  # type: ignore[arg-type]
    assert {rd.url_prefix for rd in cache.all()} == {"catalog", "dataset"}
    catalog = cache.by_prefix("catalog")
    assert catalog is not None
    (child,) = catalog.children
    assert child.target_name == "Dataset"  # cross-referenced from the store


@pytest.mark.unit
async def test_build_cache_from_empty_store_is_empty() -> None:
    cache = await build_cache_from_repository(_FakeStore(), base_url=BASE)  # type: ignore[arg-type]
    assert list(cache.all()) == []


# --- ResourceDefinitionService ---------------------------------------------


@dataclass
class _FakeReport:
    ok: bool

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise ValueError("shape violation")


@dataclass
class _FakeValidator:
    ok: bool = True
    calls: list[str] = field(default_factory=list)

    async def validate_against(self, data_graph: Graph, shape_iri: str) -> _FakeReport:
        del data_graph
        self.calls.append(shape_iri)
        return _FakeReport(self.ok)


def _service(store: _FakeStore, **kw: object) -> tuple[ResourceDefinitionService, list[int]]:
    rebuilt_sizes: list[int] = []

    async def on_rebuilt(cache: object) -> None:
        rebuilt_sizes.append(len(list(cache.all())))  # type: ignore[attr-defined]

    service = ResourceDefinitionService(
        repository=store,  # type: ignore[arg-type]
        adapter=store,  # type: ignore[arg-type]
        base_url=BASE,
        on_rebuilt=on_rebuilt,
        **kw,  # type: ignore[arg-type]
    )
    return service, rebuilt_sizes


@pytest.mark.unit
async def test_service_put_writes_rebuilds_and_notifies() -> None:
    store = _FakeStore()
    store.seed(DATASET, "dataset")  # pre-existing so catalog's child resolves
    service, rebuilt_sizes = _service(store)

    cache = await service.put(CATALOG)

    # Written to the reserved namespace and visible in the rebuilt cache.
    assert _rd_iri("catalog") in store.graphs
    assert {rd.url_prefix for rd in cache.all()} == {"catalog", "dataset"}
    assert rebuilt_sizes == [2]  # on_rebuilt fired once with the new cache


@pytest.mark.unit
async def test_service_delete_removes_and_rebuilds() -> None:
    store = _FakeStore()
    store.seed(CATALOG, "catalog")
    store.seed(DATASET, "dataset")
    service, _ = _service(store)

    cache = await service.delete(DATASET)

    assert _rd_iri("dataset") not in store.graphs
    assert {rd.url_prefix for rd in cache.all()} == {"catalog"}


@pytest.mark.unit
async def test_service_put_validates_against_rd_shape() -> None:
    store = _FakeStore()
    validator = _FakeValidator(ok=False)
    service, _ = _service(store, validator=validator)

    with pytest.raises(ValueError, match="shape violation"):
        await service.put(CATALOG)

    # Validation ran against the predefined shape and blocked the write.
    assert validator.calls == ["https://w3id.org/fdp/o#ResourceDefinitionShape"]
    assert store.graphs == {}


@pytest.mark.unit
async def test_service_root_record_iri_uses_name_slug() -> None:
    store = _FakeStore()
    service, _ = _service(store)
    root = ResourceDefinitionRecord(
        url_prefix="", name="Repository", schema_iri="https://w3id.org/fdp/o#Repository"
    )
    assert service.record_iri(root) == _rd_iri("repository")


@pytest.mark.unit
async def test_schema_exists_true_for_published_shape() -> None:
    store = _FakeStore()
    store.seed_shape(f"{DCAT}Ontology")
    service, _ = _service(store)
    assert await service.schema_exists(f"{DCAT}Ontology") is True


@pytest.mark.unit
async def test_schema_exists_false_for_missing_or_nonshape() -> None:
    store = _FakeStore()
    store.seed(CATALOG, "catalog")  # an RD record, not a SHACL shape
    service, _ = _service(store)
    # Unknown IRI → no shape.
    assert await service.schema_exists(f"{DCAT}Ontology") is False
    # A non-shape record (the RD record graph) doesn't count as a schema.
    assert await service.schema_exists(_rd_iri("catalog")) is False
