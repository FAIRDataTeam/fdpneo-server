"""Unit tests for the LDP read-extension endpoints (task 2.6).

Covers all three surfaces:

* ``/spec``, ``/{prefix}/spec``, ``/{prefix}/{id}/spec`` — type & root
  SHACL shapes; anonymous; 404 when the type or root is unknown.
* ``/expanded``, ``/{prefix}/{id}/expanded`` — record graph union'd
  with every ``dct:isPartOf`` ancestor; PDP-gated; ancestors the
  caller cannot read silently drop out.
* ``/page/{childPrefix}``, ``/{prefix}/{id}/page/{childPrefix}`` —
  paginated children listing; honours ``limit`` and ``offset``; emits
  ``X-FDP-Page-*`` headers; reports total.

We build a small FastAPI app per test with a fake repository, a fake
PDP, and a hand-rolled resource-definition cache. No live triple store
or Postgres.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from fdpneo_server.identity.deps import current_context
from fdpneo_server.metadata.extensions import build_extensions_router
from fdpneo_server.metadata.profiles.registry import (
    ChildLinkInfo,
    ResourceDefinition,
    ResourceDefinitionCache,
)
from fdpneo_server.policy.model import Decision, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import register_exception_handlers

# --- shared fixtures ------------------------------------------------------


BASE_URL = "http://localhost:8000"

REPO_SCHEMA = "https://w3id.org/fdp/o#Repository"
CATALOG_SCHEMA = "http://www.w3.org/ns/dcat#Catalog"
DATASET_SCHEMA = "http://www.w3.org/ns/dcat#Dataset"
DCAT_CATALOG = "http://www.w3.org/ns/dcat#catalog"
DCAT_DATASET = "http://www.w3.org/ns/dcat#dataset"


def _two_level_cache() -> ResourceDefinitionCache:
    """Repository → Catalog → Dataset hierarchy."""
    repo = ResourceDefinition(
        url_prefix="",
        name="Repository",
        schema_iri=REPO_SCHEMA,
        children=(
            ChildLinkInfo(
                relation_uri=DCAT_CATALOG,
                target_prefix="catalog",
                target_name="Catalog",
                target_schema_iri=CATALOG_SCHEMA,
                title="Catalogs",
                tags_uri=None,
            ),
        ),
    )
    catalog = ResourceDefinition(
        url_prefix="catalog",
        name="Catalog",
        schema_iri=CATALOG_SCHEMA,
        children=(
            ChildLinkInfo(
                relation_uri=DCAT_DATASET,
                target_prefix="dataset",
                target_name="Dataset",
                target_schema_iri=DATASET_SCHEMA,
                title="Datasets",
                tags_uri=None,
            ),
        ),
    )
    dataset = ResourceDefinition(
        url_prefix="dataset",
        name="Dataset",
        schema_iri=DATASET_SCHEMA,
        children=(),
    )
    return ResourceDefinitionCache([repo, catalog, dataset], base_url=BASE_URL)


def _shape_graph(shape_iri: str) -> Graph:
    """Build a minimal SHACL NodeShape graph for ``shape_iri``."""
    g = Graph()
    sh = URIRef("http://www.w3.org/ns/shacl#")
    g.add((URIRef(shape_iri), URIRef(sh + "targetClass"), URIRef(shape_iri)))
    return g


def _record_graph(record_iri: str, *, parent_iri: str | None = None, title: str = "") -> Graph:
    g = Graph()
    subj = URIRef(record_iri)
    if title:
        g.add((subj, DCTERMS.title, Literal(title)))
    if parent_iri:
        g.add((subj, DCTERMS.isPartOf, URIRef(parent_iri)))
    return g


class _FakeRepo:
    """In-memory ``MetadataRepository`` stand-in keyed by IRI."""

    def __init__(self, graphs: Mapping[str, Graph]) -> None:
        self._graphs = dict(graphs)

    async def get_graph(self, record_iri: Any) -> Graph:
        return self._graphs.get(str(record_iri), Graph())


class _FakePDP:
    """``PDP`` stand-in. ``denied`` set lists IRIs that should deny."""

    def __init__(self, *, denied: set[str] | None = None) -> None:
        self._denied = denied or set()

    async def authorize(self, ctx: RequestContext, action: Any, resource_iri: str) -> Decision:
        del ctx, action
        if resource_iri in self._denied:
            return Decision(outcome=Outcome.DENY, rule=None, reason="denied for test")
        return Decision(outcome=Outcome.PERMIT, rule=None, reason="permit for test")

    async def authorized_graphs(self, ctx: RequestContext, action: Any) -> set[str]:
        del ctx, action
        return set()


def _ctx(*, anonymous: bool = False) -> RequestContext:
    if anonymous:
        return RequestContext.anonymous(
            trace_id="t-1",
            request_timestamp=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        )
    return RequestContext(
        subject="https://idp.example#alice",
        roles=frozenset({"steward"}),
        trace_id="t-1",
        request_timestamp=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
    )


def _build_app(
    *,
    repo: _FakeRepo,
    pdp: _FakePDP,
    cache: ResourceDefinitionCache | None,
    ctx: RequestContext | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_extensions_router(
            repo=repo,  # type: ignore[arg-type]
            pdp=pdp,  # type: ignore[arg-type]
            cache_provider=lambda: cache,
            base_url=BASE_URL,
        )
    )
    app.dependency_overrides[current_context] = lambda: ctx or _ctx()
    return app


# --- /spec ----------------------------------------------------------------


@pytest.mark.unit
def test_root_spec_returns_shape_graph_as_turtle() -> None:
    repo = _FakeRepo({REPO_SCHEMA: _shape_graph(REPO_SCHEMA)})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/spec", headers={"Accept": "text/turtle"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/turtle")
    assert REPO_SCHEMA in response.text


@pytest.mark.unit
def test_type_spec_returns_shape_graph() -> None:
    repo = _FakeRepo({CATALOG_SCHEMA: _shape_graph(CATALOG_SCHEMA)})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/catalog/spec")
    assert response.status_code == 200
    # Turtle compresses IRIs via prefixes, so parse and assert structurally.
    g = Graph()
    g.parse(data=response.text, format="turtle")
    sh_target_class = URIRef("http://www.w3.org/ns/shacl#targetClass")
    assert (URIRef(CATALOG_SCHEMA), sh_target_class, URIRef(CATALOG_SCHEMA)) in g


@pytest.mark.unit
def test_instance_spec_returns_same_shape_as_type_spec() -> None:
    repo = _FakeRepo({CATALOG_SCHEMA: _shape_graph(CATALOG_SCHEMA)})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    type_body = TestClient(app).get("/catalog/spec").text
    instance_body = TestClient(app).get("/catalog/c-1/spec").text
    # Same content regardless of which URL was used.
    assert type_body == instance_body


@pytest.mark.unit
def test_spec_is_anonymous() -> None:
    """Anonymous callers must reach /spec — it's pre-login form data."""
    repo = _FakeRepo({CATALOG_SCHEMA: _shape_graph(CATALOG_SCHEMA)})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache(), ctx=_ctx(anonymous=True))
    response = TestClient(app).get("/catalog/spec")
    assert response.status_code == 200


@pytest.mark.unit
def test_type_spec_returns_404_for_unknown_type() -> None:
    repo = _FakeRepo({})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/bogus/spec")
    assert response.status_code == 404
    assert response.json()["code"] == "fdp.not_found"


@pytest.mark.unit
def test_root_spec_returns_404_when_cache_unloaded() -> None:
    app = _build_app(repo=_FakeRepo({}), pdp=_FakePDP(), cache=None)
    response = TestClient(app).get("/spec")
    assert response.status_code == 404


@pytest.mark.unit
def test_spec_returns_404_when_shape_graph_empty() -> None:
    """Type declared but its graph is empty — surface a clean 404."""
    repo = _FakeRepo({})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/catalog/spec")
    assert response.status_code == 404


@pytest.mark.unit
def test_spec_negotiates_jsonld() -> None:
    repo = _FakeRepo({CATALOG_SCHEMA: _shape_graph(CATALOG_SCHEMA)})
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/catalog/spec", headers={"Accept": "application/ld+json"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")


# --- /expanded -------------------------------------------------------------


@pytest.mark.unit
def test_expanded_includes_record_and_parent_via_dct_ispartof() -> None:
    catalog_iri = f"{BASE_URL}/catalog/c-1"
    repo = _FakeRepo(
        {
            catalog_iri: _record_graph(catalog_iri, parent_iri=BASE_URL + "/", title="My Catalog"),
            BASE_URL + "/": _record_graph(BASE_URL + "/", title="My Repository"),
        }
    )
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/catalog/c-1/expanded")
    assert response.status_code == 200
    # Parse the response and assert both titles are present.
    g = Graph()
    g.parse(data=response.text, format="turtle")
    titles = {str(t) for _, t in g.subject_objects(DCTERMS.title)}
    assert "My Catalog" in titles
    assert "My Repository" in titles


@pytest.mark.unit
def test_expanded_drops_ancestors_the_caller_cannot_read() -> None:
    catalog_iri = f"{BASE_URL}/catalog/c-1"
    parent_iri = BASE_URL + "/"
    repo = _FakeRepo(
        {
            catalog_iri: _record_graph(catalog_iri, parent_iri=parent_iri, title="Visible"),
            parent_iri: _record_graph(parent_iri, title="Secret"),
        }
    )
    # The parent is forbidden — it must NOT appear in the response.
    app = _build_app(
        repo=repo,
        pdp=_FakePDP(denied={parent_iri}),
        cache=_two_level_cache(),
    )
    response = TestClient(app).get("/catalog/c-1/expanded")
    assert response.status_code == 200
    g = Graph()
    g.parse(data=response.text, format="turtle")
    titles = {str(t) for _, t in g.subject_objects(DCTERMS.title)}
    assert "Visible" in titles
    assert "Secret" not in titles


@pytest.mark.unit
def test_expanded_returns_401_for_anonymous_on_protected_record() -> None:
    catalog_iri = f"{BASE_URL}/catalog/c-1"
    repo = _FakeRepo({catalog_iri: _record_graph(catalog_iri)})
    app = _build_app(
        repo=repo,
        pdp=_FakePDP(denied={catalog_iri}),
        cache=_two_level_cache(),
        ctx=_ctx(anonymous=True),
    )
    response = TestClient(app).get("/catalog/c-1/expanded")
    assert response.status_code == 401


@pytest.mark.unit
def test_expanded_returns_404_for_missing_record() -> None:
    app = _build_app(repo=_FakeRepo({}), pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/catalog/c-1/expanded")
    assert response.status_code == 404


# --- /page ----------------------------------------------------------------


def _repo_with_catalogs(n_catalogs: int) -> _FakeRepo:
    """Build a repo with ``n_catalogs`` catalogs linked from the root.

    The root's triples are keyed under the *slash-stripped* subject (``BASE_URL``,
    i.e. ``record_graph_uri`` — how the repository actually stores them), while
    the root page handler receives ``BASE_URL + "/"``. These fixtures therefore
    exercise the trailing-slash normalization the root listing depends on.
    """
    root_iri = BASE_URL  # canonical root subject, no trailing slash
    root_graph = Graph()
    root_ref = URIRef(root_iri)
    relation = URIRef(DCAT_CATALOG)
    graphs: dict[str, Graph] = {}
    for i in range(n_catalogs):
        child_iri = f"{BASE_URL}/catalog/c-{i:02d}"
        root_graph.add((root_ref, relation, URIRef(child_iri)))
        graphs[child_iri] = _record_graph(child_iri, title=f"Catalog {i}")
    graphs[root_iri] = root_graph
    return _FakeRepo(graphs)


@pytest.mark.unit
def test_page_returns_children_with_titles_and_types() -> None:
    repo = _repo_with_catalogs(3)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog")
    assert response.status_code == 200
    g = Graph()
    g.parse(data=response.text, format="turtle")
    # Three parent→child link triples, keyed by the canonical (no-slash) root.
    children = list(g.objects(URIRef(BASE_URL), URIRef(DCAT_CATALOG)))
    assert len(children) == 3
    # Each child has a dct:title and an rdf:type.
    for child in children:
        assert (child, DCTERMS.title, None) in g
        assert (child, RDF.type, URIRef(CATALOG_SCHEMA)) in g


@pytest.mark.unit
def test_page_honours_limit_and_offset() -> None:
    repo = _repo_with_catalogs(10)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog?limit=3&offset=4")
    assert response.status_code == 200
    g = Graph()
    g.parse(data=response.text, format="turtle")
    children = sorted(str(o) for o in g.objects(URIRef(BASE_URL), URIRef(DCAT_CATALOG)))
    # We asked for 3 starting at offset 4. Catalogs are sorted by IRI,
    # so c-04, c-05, c-06.
    assert children == [
        f"{BASE_URL}/catalog/c-04",
        f"{BASE_URL}/catalog/c-05",
        f"{BASE_URL}/catalog/c-06",
    ]
    assert response.headers["X-FDP-Page-Total"] == "10"
    assert response.headers["X-FDP-Page-Offset"] == "4"
    assert response.headers["X-FDP-Page-Limit"] == "3"


@pytest.mark.unit
def test_page_drops_children_the_caller_cannot_read() -> None:
    repo = _repo_with_catalogs(3)
    forbidden = f"{BASE_URL}/catalog/c-01"
    app = _build_app(repo=repo, pdp=_FakePDP(denied={forbidden}), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog")
    assert response.status_code == 200
    g = Graph()
    g.parse(data=response.text, format="turtle")
    children = sorted(str(o) for o in g.objects(URIRef(BASE_URL), URIRef(DCAT_CATALOG)))
    assert forbidden not in children
    # Total still reports 3 — the page shrunk, not the underlying count.
    assert response.headers["X-FDP-Page-Total"] == "3"


@pytest.mark.unit
def test_page_rejects_unknown_child_prefix() -> None:
    repo = _repo_with_catalogs(1)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/widget")
    assert response.status_code == 404
    assert response.json()["code"] == "fdp.not_found"


@pytest.mark.unit
def test_page_limit_validated() -> None:
    app = _build_app(repo=_FakeRepo({}), pdp=_FakePDP(), cache=_two_level_cache())
    # FastAPI returns 422 for query-param validation failures.
    assert TestClient(app).get("/page/catalog?limit=0").status_code == 422
    assert TestClient(app).get("/page/catalog?limit=2000").status_code == 422
    assert TestClient(app).get("/page/catalog?offset=-1").status_code == 422


@pytest.mark.unit
def test_instance_page_lists_grandchildren() -> None:
    """Catalog → Dataset path: /catalog/c-1/page/dataset returns datasets."""
    catalog_iri = f"{BASE_URL}/catalog/c-1"
    dataset_iri = f"{BASE_URL}/dataset/d-1"
    catalog_graph = Graph()
    catalog_graph.add((URIRef(catalog_iri), URIRef(DCAT_DATASET), URIRef(dataset_iri)))
    repo = _FakeRepo(
        {
            catalog_iri: catalog_graph,
            dataset_iri: _record_graph(dataset_iri, title="D1"),
        }
    )
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/catalog/c-1/page/dataset")
    assert response.status_code == 200
    g = Graph()
    g.parse(data=response.text, format="turtle")
    assert (URIRef(catalog_iri), URIRef(DCAT_DATASET), URIRef(dataset_iri)) in g


@pytest.mark.unit
def test_root_page_normalizes_trailing_slash_to_find_children() -> None:
    """Regression: the root page must query the slash-stripped root subject.

    The repository stores the root's triples under ``BASE_URL`` (no trailing
    slash, via ``record_graph_uri``), but the ``/page`` handler receives the
    root as ``BASE_URL + "/"``. Without normalizing the subject the forward-link
    lookup matches nothing and reports zero children even though catalogs exist.
    """
    repo = _repo_with_catalogs(4)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog")
    assert response.status_code == 200
    assert response.headers["X-FDP-Page-Total"] == "4"
    g = Graph()
    g.parse(data=response.text, format="turtle")
    children = list(g.objects(URIRef(BASE_URL), URIRef(DCAT_CATALOG)))
    assert len(children) == 4


# --- /page RFC 8288 navigation links (ADR-0022 §1) -------------------------


def _link_rels(header: str) -> dict[str, str]:
    """Parse an RFC 8288 ``Link`` header into ``{rel: target}`` (targets are
    URL-encoded query strings, so commas never appear inside a value)."""
    rels: dict[str, str] = {}
    for part in header.split(","):
        target = re.search(r"<([^>]*)>", part)
        rel = re.search(r'rel="([^"]*)"', part)
        if target and rel:
            rels[rel.group(1)] = target.group(1)
    return rels


def _offset_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["offset"][0]


@pytest.mark.unit
def test_page_first_page_has_no_prev_link() -> None:
    repo = _repo_with_catalogs(10)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog?limit=3&offset=0")
    rels = _link_rels(response.headers["Link"])
    assert set(rels) == {"first", "next", "last"}
    assert _offset_of(rels["first"]) == "0"
    assert _offset_of(rels["next"]) == "3"
    assert _offset_of(rels["last"]) == "9"  # ((10-1)//3)*3


@pytest.mark.unit
def test_page_middle_page_has_all_four_links() -> None:
    repo = _repo_with_catalogs(10)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog?limit=3&offset=3")
    rels = _link_rels(response.headers["Link"])
    assert set(rels) == {"first", "prev", "next", "last"}
    assert _offset_of(rels["first"]) == "0"
    assert _offset_of(rels["prev"]) == "0"
    assert _offset_of(rels["next"]) == "6"
    assert _offset_of(rels["last"]) == "9"


@pytest.mark.unit
def test_page_last_page_has_no_next_link() -> None:
    repo = _repo_with_catalogs(10)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog?limit=3&offset=9")
    rels = _link_rels(response.headers["Link"])
    assert set(rels) == {"first", "prev", "last"}
    assert _offset_of(rels["prev"]) == "6"
    assert _offset_of(rels["last"]) == "9"


@pytest.mark.unit
def test_page_single_page_has_only_first_and_last() -> None:
    repo = _repo_with_catalogs(3)  # default limit 50 → everything on one page
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog")
    rels = _link_rels(response.headers["Link"])
    assert set(rels) == {"first", "last"}
    assert _offset_of(rels["first"]) == "0"
    assert _offset_of(rels["last"]) == "0"


@pytest.mark.unit
def test_page_links_preserve_other_query_params() -> None:
    repo = _repo_with_catalogs(10)
    app = _build_app(repo=repo, pdp=_FakePDP(), cache=_two_level_cache())
    response = TestClient(app).get("/page/catalog?limit=3&offset=3&foo=bar")
    rels = _link_rels(response.headers["Link"])
    for target in rels.values():
        params = parse_qs(urlsplit(target).query)
        assert params["limit"] == ["3"]  # caller's limit carried through
        assert params["foo"] == ["bar"]  # arbitrary params preserved
        assert "offset" in params  # only offset is rewritten per link
