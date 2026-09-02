"""Unit tests for the LDP router skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdpneo_server.identity.deps import current_context
from fdpneo_server.metadata.etag import compute_etag
from fdpneo_server.metadata.events import RecordCreated, RecordDeleted, RecordModified
from fdpneo_server.metadata.graphs import meta_graph_uri
from fdpneo_server.metadata.ldp import ContainerRegistry, build_ldp_router
from fdpneo_server.metadata.repository import MetadataRepository
from fdpneo_server.metadata.shacl import InMemoryShapeProvider, ShaclValidator
from fdpneo_server.metadata.signposting import (
    REL_HAS_EXPANDED_VIEW,
    REL_HAS_MEMBER_PAGE,
    REL_HAS_META_METADATA,
    REL_HAS_RESOURCE_DEFINITIONS,
    REL_HAS_SPEC,
    REL_HAS_STATE_TRANSITION,
    Link,
)
from fdpneo_server.policy.model import Action, Decision, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import register_exception_handlers
from fdpneo_server.shared.events import EventBus
from fdpneo_server.shared.namespaces import (
    DCT,
    FDP_ALLOWED_STATE_TRANSITION,
    FDP_METADATA_STATE,
    LDP,
    OWL,
)
from fdpneo_server.shared.negotiation import (
    JSON_LD,
    N_TRIPLES,
    RDF_XML,
    SPARQL_UPDATE,
    TURTLE,
)

RECORD_PATH = "/ldp/catalogs/c1"
CONTAINER_PATH = "/ldp/catalogs"
RECORD_IRI = "http://testserver/ldp/catalogs/c1"
CONTAINER_IRI = "http://testserver/ldp/catalogs"
ALICE = "https://idp.example/realms/fdp#alice"

DATASET_SHAPE_IRI = "https://example.org/shapes/dataset"
DATASET_SHAPE_TTL = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

<https://example.org/shapes/dataset>
    a sh:NodeShape ;
    sh:targetClass dcat:Dataset ;
    sh:property [
        sh:path dct:title ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""


# --- Test doubles -----------------------------------------------------------


def _empty_graphs() -> dict[str, Graph]:
    return {}


def _empty_calls() -> list[str]:
    return []


@dataclass
class FakeAdapter:
    """Minimal in-memory triple store stand-in, mirroring test_repository.py."""

    graphs: dict[str, Graph] = field(default_factory=_empty_graphs)
    update_calls: list[str] = field(default_factory=_empty_calls)

    async def query(self, sparql: str, *, accept: str = "application/sparql-results+json") -> bytes:
        del accept
        target = _extract_graph_uri(sparql)
        if target is None or target not in self.graphs:
            return b""
        return self.graphs[target].serialize(format="turtle").encode("utf-8")

    async def update(self, sparql: str) -> None:
        self.update_calls.append(sparql)

    async def replace_graph(
        self, graph_uri: str, data: bytes | str | Graph, *, mime: str = "text/turtle"
    ) -> None:
        del mime
        new = Graph()
        if isinstance(data, Graph):
            for triple in data:
                new.add(triple)
        else:
            blob = data.decode("utf-8") if isinstance(data, bytes) else data
            new.parse(data=blob, format="nt")
        self.graphs[graph_uri] = new

    async def drop_graph(self, graph_uri: str) -> None:
        self.graphs.pop(graph_uri, None)


def _extract_graph_uri(sparql: str) -> str | None:
    marker = "GRAPH <"
    start = sparql.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = sparql.find(">", start)
    return sparql[start:end] if end != -1 else None


def _empty_overrides() -> dict[tuple[str, str], Outcome]:
    return {}


def _empty_call_log() -> list[tuple[str, str]]:
    return []


@dataclass
class FakePDP:
    """Test PDP: default outcome with optional (action, iri) overrides."""

    default: Outcome = Outcome.PERMIT
    overrides: dict[tuple[str, str], Outcome] = field(default_factory=_empty_overrides)
    calls: list[tuple[str, str]] = field(default_factory=_empty_call_log)

    async def authorize(self, ctx: RequestContext, action: Action, resource_iri: str) -> Decision:
        del ctx
        self.calls.append((action.value, resource_iri))
        outcome = self.overrides.get((action.value, resource_iri), self.default)
        return Decision(outcome=outcome, rule=None, reason="test")


class FixedContainerRegistry:
    """Container registry that flags a fixed set of IRIs and shape bindings.

    ``member_shapes`` maps container IRI → shape IRI for *new members* (POST).
    ``resource_shapes`` maps resource IRI → shape IRI for the resource itself
    (PATCH).
    """

    def __init__(
        self,
        container_iris: set[str],
        member_shapes: dict[str, str] | None = None,
        resource_shapes: dict[str, str] | None = None,
        relations: dict[tuple[str, str], str] | None = None,
        member_relations: dict[str, list[str]] | None = None,
        url_prefixes: dict[str, str] | None = None,
        child_prefixes: dict[str, list[str]] | None = None,
    ) -> None:
        self._containers = set(container_iris)
        self._members = dict(member_shapes or {})
        self._resources = dict(resource_shapes or {})
        self._relations = dict(relations or {})
        self._member_relations = dict(member_relations or {})
        self._url_prefixes = dict(url_prefixes or {})
        self._child_prefixes = dict(child_prefixes or {})

    def is_container(self, resource_iri: str) -> bool:
        return resource_iri in self._containers

    def member_shape(self, container_iri: str) -> str | None:
        return self._members.get(container_iri)

    def shape_for(self, resource_iri: str) -> str | None:
        return self._resources.get(resource_iri)

    def containment_relation(self, parent_iri: str, child_iri: str) -> str | None:
        return self._relations.get((parent_iri, child_iri))

    def member_relations(self, resource_iri: str) -> list[str]:
        return self._member_relations.get(resource_iri, [])

    def url_prefix_for(self, resource_iri: str) -> str | None:
        return self._url_prefixes.get(resource_iri)

    def child_prefixes(self, resource_iri: str) -> list[str]:
        return self._child_prefixes.get(resource_iri, [])


# --- Fixtures ---------------------------------------------------------------


def _authenticated_ctx() -> RequestContext:
    return RequestContext(
        subject=ALICE,
        roles=frozenset({"steward"}),
        request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        trace_id="t-1",
    )


def _anonymous_ctx() -> RequestContext:
    return RequestContext.anonymous(
        trace_id="t-1",
        request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )


def _make_repo(adapter: FakeAdapter | None = None) -> tuple[MetadataRepository, FakeAdapter]:
    a = adapter or FakeAdapter()
    return MetadataRepository(a), a  # type: ignore[arg-type]


def _build_app(
    *,
    repo: MetadataRepository,
    pdp: FakePDP,
    ctx: RequestContext | None = None,
    containers: ContainerRegistry | None = None,
    validator: ShaclValidator | None = None,
    triplestore: FakeAdapter | None = None,
    event_bus: EventBus | None = None,
    root_service_links: list[Link] | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_ldp_router(
            repo=repo,  # type: ignore[arg-type]
            pdp=pdp,  # type: ignore[arg-type]
            validator=validator,
            containers=containers,
            triplestore=triplestore,  # type: ignore[arg-type]
            event_bus=event_bus,
            root_service_links=root_service_links,
        )
    )
    app.dependency_overrides[current_context] = lambda: ctx or _authenticated_ctx()
    return app


async def _seed_record(repo: MetadataRepository, iri: str, *, title: str = "hello") -> str:
    graph = Graph()
    graph.add((URIRef(iri), DCT.title, Literal(title)))
    return await repo.put_graph(iri, graph, subject=ALICE)


# --- GET / HEAD -------------------------------------------------------------


@pytest.mark.unit
async def test_get_returns_turtle_with_etag_and_link_headers() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    pdp = FakePDP()
    app = _build_app(repo=repo, pdp=pdp)
    with TestClient(app) as client:
        response = client.get(RECORD_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(TURTLE)
    assert response.headers["etag"].startswith('"') and response.headers["etag"].endswith('"')
    assert "ldp#Resource" in response.headers["link"]
    assert "ldp#RDFSource" in response.headers["link"]
    assert "ldp#DirectContainer" not in response.headers["link"]
    assert "hello" in response.text
    assert pdp.calls == [("read", RECORD_IRI)]


@pytest.mark.unit
async def test_authorization_normalises_trailing_slash_iri() -> None:
    # Regression: the repository root is addressed as ".../" but stored (and its
    # `dct:rights` offer keyed) at the no-slash IRI. Authorization must use the
    # canonical IRI, else writes to the root are default-denied even for stewards.
    repo, _ = _make_repo()
    pdp = FakePDP()
    app = _build_app(repo=repo, pdp=pdp)
    with TestClient(app) as client:
        client.get("/ldp/catalogs/")  # trailing slash; empty repo → 404 after authz

    assert pdp.calls == [("read", "http://testserver/ldp/catalogs")]


@pytest.mark.unit
async def test_get_with_json_ld_accept_returns_json_ld() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.get(RECORD_PATH, headers={"accept": JSON_LD})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(JSON_LD)


@pytest.mark.unit
async def test_get_unsupported_accept_returns_406() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.get(RECORD_PATH, headers={"accept": "text/csv"})

    assert response.status_code == 406
    assert response.json()["code"] == "fdp.not_acceptable"


@pytest.mark.unit
async def test_get_missing_resource_returns_404() -> None:
    repo, _ = _make_repo()
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.get(RECORD_PATH)
    assert response.status_code == 404
    assert response.json()["code"] == "fdp.not_found"


@pytest.mark.unit
async def test_get_denied_for_authenticated_subject_returns_403() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    pdp = FakePDP(default=Outcome.DENY)
    app = _build_app(repo=repo, pdp=pdp)
    with TestClient(app) as client:
        response = client.get(RECORD_PATH)

    assert response.status_code == 403
    assert response.json()["code"] == "fdp.policy_violation"


@pytest.mark.unit
async def test_get_denied_for_anonymous_returns_401() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    pdp = FakePDP(default=Outcome.DENY)
    app = _build_app(repo=repo, pdp=pdp, ctx=_anonymous_ctx())
    with TestClient(app) as client:
        response = client.get(RECORD_PATH)

    assert response.status_code == 401
    assert response.json()["code"] == "fdp.unauthenticated"


@pytest.mark.unit
async def test_head_returns_headers_without_body() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.head(RECORD_PATH)

    assert response.status_code == 200
    assert response.content == b""
    assert "etag" in response.headers
    assert "link" in response.headers


# --- Signposting (ADR-0017 §2) ---------------------------------------------


@pytest.mark.unit
async def test_get_signposting_cite_as_defaults_to_canonical() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        resp = client.get(RECORD_PATH, headers={"Accept": "text/turtle"})
    assert resp.status_code == 200
    link = resp.headers["link"]
    assert f'<{RECORD_IRI}>; rel="cite-as"' in link
    # LDP type links still come first; describedby is emitted per RDF media type.
    assert f'<{LDP.Resource}>; rel="type"' in link
    assert f'<{RECORD_IRI}>; rel="describedby"; type="text/turtle"' in link


@pytest.mark.unit
async def test_get_signposting_cite_as_prefers_client_doi_sameas() -> None:
    repo, _ = _make_repo()
    doi = "https://doi.org/10.1234/foo"
    graph = Graph()
    graph.add((URIRef(RECORD_IRI), DCT.title, Literal("titled")))
    graph.add((URIRef(RECORD_IRI), OWL.sameAs, URIRef(doi)))
    await repo.put_graph(RECORD_IRI, graph, subject=ALICE)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        resp = client.get(RECORD_PATH, headers={"Accept": "text/turtle"})
    link = resp.headers["link"]
    assert f'<{doi}>; rel="cite-as"' in link
    assert f'<{RECORD_IRI}>; rel="cite-as"' not in link


@pytest.mark.unit
async def test_get_container_carries_item_links() -> None:
    repo, _ = _make_repo()
    child = f"{CONTAINER_IRI}/child-1"
    graph = Graph()
    graph.add((URIRef(CONTAINER_IRI), DCT.title, Literal("container")))
    graph.add((URIRef(CONTAINER_IRI), LDP.contains, URIRef(child)))
    await repo.put_graph(CONTAINER_IRI, graph, subject=ALICE)
    containers = FixedContainerRegistry(container_iris={CONTAINER_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    with TestClient(app) as client:
        resp = client.get(CONTAINER_PATH, headers={"Accept": "text/turtle"})
    assert resp.status_code == 200
    assert f'<{child}>; rel="item"' in resp.headers["link"]


@pytest.mark.unit
async def test_get_record_advertises_affordance_links() -> None:
    """A record of a declared type advertises its management views (ADR-0022 §2)."""
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    containers = FixedContainerRegistry(container_iris=set(), url_prefixes={RECORD_IRI: "catalogs"})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    with TestClient(app) as client:
        link = client.get(RECORD_PATH, headers={"Accept": "text/turtle"}).headers["link"]
    assert f'<{RECORD_IRI}/meta>; rel="{REL_HAS_META_METADATA}"' in link
    assert f'<{RECORD_IRI}/spec>; rel="{REL_HAS_SPEC}"' in link
    assert f'<{RECORD_IRI}/expanded>; rel="{REL_HAS_EXPANDED_VIEW}"' in link
    assert f'<{RECORD_IRI}/state>; rel="{REL_HAS_STATE_TRANSITION}"' in link
    # No children declared → no member-page affordance.
    assert REL_HAS_MEMBER_PAGE not in link


@pytest.mark.unit
async def test_get_container_advertises_member_page_per_child() -> None:
    repo, _ = _make_repo()
    graph = Graph()
    graph.add((URIRef(CONTAINER_IRI), DCT.title, Literal("container")))
    await repo.put_graph(CONTAINER_IRI, graph, subject=ALICE)
    containers = FixedContainerRegistry(
        container_iris={CONTAINER_IRI},
        url_prefixes={CONTAINER_IRI: "catalogs"},
        child_prefixes={CONTAINER_IRI: ["dataset", "service"]},
    )
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    with TestClient(app) as client:
        link = client.get(CONTAINER_PATH, headers={"Accept": "text/turtle"}).headers["link"]
    assert f'<{CONTAINER_IRI}/page/dataset>; rel="{REL_HAS_MEMBER_PAGE}"' in link
    assert f'<{CONTAINER_IRI}/page/service>; rel="{REL_HAS_MEMBER_PAGE}"' in link


@pytest.mark.unit
async def test_affordance_links_survive_item_trimming() -> None:
    """Affordances are fixed relations: they are kept even when a huge container's
    ``item`` links are trimmed to fit MAX_LINKS (ADR-0022 §2)."""
    repo, _ = _make_repo()
    graph = Graph()
    graph.add((URIRef(CONTAINER_IRI), DCT.title, Literal("big")))
    for i in range(100):
        graph.add((URIRef(CONTAINER_IRI), LDP.contains, URIRef(f"{CONTAINER_IRI}/d{i:03d}")))
    await repo.put_graph(CONTAINER_IRI, graph, subject=ALICE)
    containers = FixedContainerRegistry(
        container_iris={CONTAINER_IRI},
        url_prefixes={CONTAINER_IRI: "catalogs"},
        child_prefixes={CONTAINER_IRI: ["dataset"]},
    )
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    with TestClient(app) as client:
        link = client.get(CONTAINER_PATH, headers={"Accept": "text/turtle"}).headers["link"]
    # Every affordance relation is present despite item trimming.
    for rel in (
        REL_HAS_META_METADATA,
        REL_HAS_SPEC,
        REL_HAS_EXPANDED_VIEW,
        REL_HAS_STATE_TRANSITION,
        REL_HAS_MEMBER_PAGE,
    ):
        assert f'rel="{rel}"' in link
    # The combined Link set is still bounded (items were trimmed, not affordances).
    assert link.count('rel="item"') < 100


@pytest.mark.unit
async def test_no_affordance_links_on_missing_or_denied_get() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    containers = FixedContainerRegistry(container_iris=set(), url_prefixes={RECORD_IRI: "catalogs"})
    # 404: a missing record carries no affordance advertisement.
    missing = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    with TestClient(missing) as client:
        r404 = client.get("/ldp/catalogs/nope")
    assert r404.status_code == 404
    assert REL_HAS_META_METADATA not in r404.headers.get("link", "")
    # 401: anonymous denial carries no affordance advertisement either.
    denied = _build_app(
        repo=repo, pdp=FakePDP(default=Outcome.DENY), ctx=_anonymous_ctx(), containers=containers
    )
    with TestClient(denied) as client:
        r401 = client.get(RECORD_PATH)
    assert r401.status_code == 401
    assert REL_HAS_META_METADATA not in r401.headers.get("link", "")


@pytest.mark.unit
async def test_root_record_advertises_api_description_links() -> None:
    """The root FDP record (empty url_prefix) advertises the API description
    (ADR-0022 §4); non-root records do not."""
    root_links = [
        Link("/fdp-api/openapi.json", "service-desc"),
        Link("/fdp-api/resource-definitions", REL_HAS_RESOURCE_DEFINITIONS),
        Link("/fdp-api/docs", "service-doc"),
    ]
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    # Registry reports this record as the root (url_prefix == "").
    root_reg = FixedContainerRegistry(container_iris=set(), url_prefixes={RECORD_IRI: ""})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=root_reg, root_service_links=root_links)
    with TestClient(app) as client:
        link = client.get(RECORD_PATH, headers={"Accept": "text/turtle"}).headers["link"]
    assert '</fdp-api/openapi.json>; rel="service-desc"' in link
    assert '</fdp-api/docs>; rel="service-doc"' in link
    assert f'</fdp-api/resource-definitions>; rel="{REL_HAS_RESOURCE_DEFINITIONS}"' in link

    # A non-root record (url_prefix "catalogs") gets none of the API-description rels.
    nonroot_reg = FixedContainerRegistry(
        container_iris=set(), url_prefixes={RECORD_IRI: "catalogs"}
    )
    app2 = _build_app(
        repo=repo, pdp=FakePDP(), containers=nonroot_reg, root_service_links=root_links
    )
    with TestClient(app2) as client:
        link2 = client.get(RECORD_PATH, headers={"Accept": "text/turtle"}).headers["link"]
    assert "service-desc" not in link2
    assert REL_HAS_RESOURCE_DEFINITIONS not in link2


@pytest.mark.unit
async def test_meta_get_carries_allowed_state_transition_view_triples() -> None:
    """GET <record>/meta advertises the record's next states (ADR-0022 §3), and
    the stored meta graph is left untouched (so ``fdp dump`` excludes them)."""
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)  # meta writer stamps DRAFT
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        resp = client.get(f"{RECORD_PATH}/meta", headers={"Accept": "text/turtle"})
    assert resp.status_code == 200
    served = Graph()
    served.parse(data=resp.text, format="turtle")
    # DRAFT's only successor is PUBLISHED.
    successors = {str(o) for o in served.objects(URIRef(RECORD_IRI), FDP_ALLOWED_STATE_TRANSITION)}
    assert successors == {"PUBLISHED"}
    assert (URIRef(RECORD_IRI), FDP_METADATA_STATE, Literal("DRAFT")) in served
    # The persisted meta graph never gains the view triples (dump reads storage).
    stored = await repo.get_meta(RECORD_IRI)
    assert (URIRef(RECORD_IRI), FDP_ALLOWED_STATE_TRANSITION, None) not in stored


@pytest.mark.unit
async def test_head_carries_the_same_link_header_as_get() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        get_link = client.get(RECORD_PATH, headers={"Accept": "text/turtle"}).headers["link"]
        head_link = client.head(RECORD_PATH, headers={"Accept": "text/turtle"}).headers["link"]
    assert head_link == get_link


# --- PUT --------------------------------------------------------------------


@pytest.mark.unit
async def test_put_new_resource_returns_201_with_location() -> None:
    repo, _ = _make_repo()
    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "new" .'.encode()
    with TestClient(app) as client:
        response = client.put(RECORD_PATH, content=body, headers={"content-type": N_TRIPLES})

    assert response.status_code == 201
    assert response.headers["location"] == RECORD_IRI
    assert "etag" in response.headers
    stored = await repo.get_graph(RECORD_IRI)
    assert (URIRef(RECORD_IRI), DCT.title, Literal("new")) in stored


@pytest.mark.unit
async def test_put_replace_requires_if_match_header() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "updated" .'.encode()
    with TestClient(app) as client:
        response = client.put(RECORD_PATH, content=body, headers={"content-type": N_TRIPLES})

    assert response.status_code == 428
    assert response.json()["code"] == "fdp.precondition_required"


@pytest.mark.unit
async def test_put_replace_with_stale_if_match_returns_412() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "updated" .'.encode()
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH,
            content=body,
            headers={"content-type": N_TRIPLES, "if-match": '"not-the-etag"'},
        )

    assert response.status_code == 412
    assert response.json()["code"] == "fdp.precondition_failed"


@pytest.mark.unit
async def test_put_replace_with_matching_if_match_succeeds() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    current = await repo.get_graph(RECORD_IRI)
    etag = compute_etag(current)

    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "updated" .'.encode()
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH,
            content=body,
            headers={"content-type": N_TRIPLES, "if-match": f'"{etag}"'},
        )

    assert response.status_code == 200


@pytest.mark.unit
async def test_put_replace_accepts_proxy_coding_suffixed_if_match() -> None:
    """A compressing edge rewrites the ETag on the wire ("abc" → "abc-zstd" for
    Caddy's encode, "abc-gzip" for Apache mod_deflate); the client faithfully
    round-trips what it saw, so If-Match arrives suffixed. The comparison must
    tolerate a known coding suffix — seen live: every edit 412'd behind Caddy."""
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    current = await repo.get_graph(RECORD_IRI)
    etag = compute_etag(current)

    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "updated" .'.encode()
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH,
            content=body,
            headers={"content-type": N_TRIPLES, "if-match": f'"{etag}-zstd"'},
        )

    assert response.status_code == 200


@pytest.mark.unit
async def test_put_replace_with_stale_suffixed_if_match_still_412s() -> None:
    """Tolerating the coding suffix must not weaken concurrency: a *stale* ETag
    with a coding suffix is still rejected."""
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)

    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "updated" .'.encode()
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH,
            content=body,
            headers={"content-type": N_TRIPLES, "if-match": '"not-the-etag-gzip"'},
        )

    assert response.status_code == 412


@pytest.mark.unit
async def test_put_unsupported_content_type_returns_415() -> None:
    repo, _ = _make_repo()
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH, content=b"<x>", headers={"content-type": "application/json"}
        )

    assert response.status_code == 415
    assert response.json()["code"] == "fdp.unsupported_media_type"


@pytest.mark.unit
async def test_put_invalid_against_resource_shape_returns_422() -> None:
    repo, _ = _make_repo()
    validator = ShaclValidator(InMemoryShapeProvider({DATASET_SHAPE_IRI: DATASET_SHAPE_TTL}))
    # PUT validates the resource against its *own* type shape (shape_for),
    # not the container's member shape.
    containers = FixedContainerRegistry(
        container_iris=set(),
        resource_shapes={RECORD_IRI: DATASET_SHAPE_IRI},
    )
    app = _build_app(repo=repo, pdp=FakePDP(), validator=validator, containers=containers)
    # Body declares a dcat:Dataset but omits the required dct:title.
    body = (
        f"<{RECORD_IRI}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://www.w3.org/ns/dcat#Dataset> ."
    ).encode()
    with TestClient(app) as client:
        response = client.put(RECORD_PATH, content=body, headers={"content-type": N_TRIPLES})

    assert response.status_code == 422
    assert response.json()["code"] == "fdp.schema_violation"


@pytest.mark.unit
async def test_put_conforming_against_resource_shape_succeeds() -> None:
    repo, _ = _make_repo()
    validator = ShaclValidator(InMemoryShapeProvider({DATASET_SHAPE_IRI: DATASET_SHAPE_TTL}))
    containers = FixedContainerRegistry(
        container_iris=set(),
        resource_shapes={RECORD_IRI: DATASET_SHAPE_IRI},
    )
    app = _build_app(repo=repo, pdp=FakePDP(), validator=validator, containers=containers)
    body = (
        f"<{RECORD_IRI}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://www.w3.org/ns/dcat#Dataset> .\n"
        f'<{RECORD_IRI}> <http://purl.org/dc/terms/title> "Genome Dataset" .'
    ).encode()
    with TestClient(app) as client:
        response = client.put(RECORD_PATH, content=body, headers={"content-type": N_TRIPLES})
    assert response.status_code == 201


# --- POST -------------------------------------------------------------------


@pytest.mark.unit
async def test_post_to_non_container_returns_405() -> None:
    repo, _ = _make_repo()
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.post(
            RECORD_PATH, content=b"<x> <y> <z> .", headers={"content-type": N_TRIPLES}
        )
    assert response.status_code == 405
    assert response.json()["code"] == "fdp.method_not_allowed"


@pytest.mark.unit
async def test_post_to_container_mints_member_uri_from_slug() -> None:
    repo, _ = _make_repo()
    containers = FixedContainerRegistry(container_iris={CONTAINER_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    body = b'<urn:_> <http://purl.org/dc/terms/title> "child" .'
    with TestClient(app) as client:
        response = client.post(
            CONTAINER_PATH,
            content=body,
            headers={"content-type": N_TRIPLES, "slug": "biobank-data"},
        )

    assert response.status_code == 201
    assert response.headers["location"] == f"{CONTAINER_IRI}/biobank-data"


@pytest.mark.unit
async def test_post_without_slug_generates_uuid_path_segment() -> None:
    repo, _ = _make_repo()
    containers = FixedContainerRegistry(container_iris={CONTAINER_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    body = b'<urn:_> <http://purl.org/dc/terms/title> "child" .'
    with TestClient(app) as client:
        response = client.post(CONTAINER_PATH, content=body, headers={"content-type": N_TRIPLES})

    assert response.status_code == 201
    location = response.headers["location"]
    assert location.startswith(CONTAINER_IRI + "/")
    # The mint-by-uuid path is 36 hex/dashes.
    assert len(location[len(CONTAINER_IRI) + 1 :]) == 36


@pytest.mark.unit
async def test_post_slug_collision_returns_409() -> None:
    """POST never overwrites: a Slug matching an existing record → 409 (ADR-0016 §1)."""
    repo, _ = _make_repo()
    # A record already lives at the slug-derived member IRI.
    await _seed_record(repo, f"{CONTAINER_IRI}/biobank-data", title="existing")
    containers = FixedContainerRegistry(container_iris={CONTAINER_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    body = b'<urn:_> <http://purl.org/dc/terms/title> "child" .'
    with TestClient(app) as client:
        response = client.post(
            CONTAINER_PATH,
            content=body,
            headers={"content-type": N_TRIPLES, "slug": "biobank-data"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "fdp.conflict"


# --- PATCH ------------------------------------------------------------------


@pytest.mark.unit
async def test_patch_wrong_content_type_returns_415() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH, content=b"INSERT DATA { <x> <y> <z> }", headers={"content-type": TURTLE}
        )
    assert response.status_code == 415


@pytest.mark.unit
async def test_patch_applies_simulated_update_and_returns_204() -> None:
    repo, adapter = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    current_etag = compute_etag(await repo.get_graph(RECORD_IRI))

    app = _build_app(repo=repo, pdp=FakePDP())
    sparql = f'INSERT DATA {{ <> <{DCT.description}> "added" }}'
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH,
            content=sparql.encode("utf-8"),
            headers={"content-type": SPARQL_UPDATE, "if-match": f'"{current_etag}"'},
        )

    assert response.status_code == 204
    # Simulated locally then committed via replace_graph — not raw update.
    assert adapter.update_calls == []
    stored = adapter.graphs[RECORD_IRI]
    assert (URIRef(RECORD_IRI), DCT.description, Literal("added")) in stored
    meta = adapter.graphs[str(meta_graph_uri(RECORD_IRI))]
    assert any(p == DCT.modified for _, p, _ in meta)


@pytest.mark.unit
async def test_patch_failing_post_state_shacl_returns_422_and_leaves_storage_unchanged() -> None:
    repo, adapter = _make_repo()
    # Seed a Dataset record with the required dct:title.
    seed = Graph()
    seed.add((URIRef(RECORD_IRI), RDF.type, URIRef("http://www.w3.org/ns/dcat#Dataset")))
    seed.add((URIRef(RECORD_IRI), DCT.title, Literal("ok")))
    await repo.put_graph(RECORD_IRI, seed, subject=ALICE)
    snapshot = sorted(str(t) for t in adapter.graphs[RECORD_IRI])
    current_etag = compute_etag(await repo.get_graph(RECORD_IRI))

    validator = ShaclValidator(InMemoryShapeProvider({DATASET_SHAPE_IRI: DATASET_SHAPE_TTL}))
    containers = FixedContainerRegistry(
        container_iris=set(),
        resource_shapes={RECORD_IRI: DATASET_SHAPE_IRI},
    )
    app = _build_app(repo=repo, pdp=FakePDP(), validator=validator, containers=containers)

    # Delete the required title — post-update graph should fail SHACL.
    sparql = f'DELETE DATA {{ <> <{DCT.title}> "ok" }}'
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH,
            content=sparql.encode("utf-8"),
            headers={"content-type": SPARQL_UPDATE, "if-match": f'"{current_etag}"'},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "fdp.schema_violation"
    # Storage unchanged.
    assert sorted(str(t) for t in adapter.graphs[RECORD_IRI]) == snapshot


@pytest.mark.unit
async def test_patch_rejects_body_with_service_clause() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))
    app = _build_app(repo=repo, pdp=FakePDP())
    sparql = (
        "INSERT { ?s <http://example.org/p> ?o } WHERE { "
        "SERVICE <http://attacker.example/> { ?s ?p ?o } }"
    )
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH,
            content=sparql.encode("utf-8"),
            headers={"content-type": SPARQL_UPDATE, "if-match": f'"{etag}"'},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
async def test_patch_malformed_sparql_returns_400() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH,
            content=b"INSERT DATA { <oops",
            headers={"content-type": SPARQL_UPDATE, "if-match": f'"{etag}"'},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "fdp.bad_request"


@pytest.mark.unit
async def test_patch_publishes_record_modified_event_on_success() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))

    bus = EventBus()
    received: list[RecordModified] = []

    async def handler(evt: RecordModified) -> None:
        received.append(evt)

    sub = bus.subscribe(RecordModified, handler)
    try:
        app = _build_app(repo=repo, pdp=FakePDP(), event_bus=bus)
        sparql = f'INSERT DATA {{ <> <{DCT.description}> "extra" }}'
        with TestClient(app) as client:
            response = client.patch(
                RECORD_PATH,
                content=sparql.encode("utf-8"),
                headers={"content-type": SPARQL_UPDATE, "if-match": f'"{etag}"'},
            )
    finally:
        sub.unsubscribe()

    assert response.status_code == 204
    assert len(received) == 1
    evt = received[0]
    assert evt.record_iri == RECORD_IRI
    assert evt.subject == ALICE
    new_etag = response.headers["etag"].strip('"')
    assert evt.etag == new_etag


@pytest.mark.unit
async def test_patch_does_not_publish_event_on_failure() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))

    bus = EventBus()
    received: list[RecordModified] = []

    async def handler(evt: RecordModified) -> None:
        received.append(evt)

    sub = bus.subscribe(RecordModified, handler)
    try:
        app = _build_app(repo=repo, pdp=FakePDP(), event_bus=bus)
        with TestClient(app) as client:
            response = client.patch(
                RECORD_PATH,
                content=b"INSERT DATA { <oops",
                headers={"content-type": SPARQL_UPDATE, "if-match": f'"{etag}"'},
            )
    finally:
        sub.unsubscribe()

    assert response.status_code == 400
    assert received == []


@pytest.mark.unit
async def test_patch_missing_if_match_returns_428() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH,
            content=b"INSERT DATA { <x> <y> <z> }",
            headers={"content-type": SPARQL_UPDATE},
        )
    assert response.status_code == 428


# --- DELETE -----------------------------------------------------------------


@pytest.mark.unit
async def test_delete_requires_if_match_and_then_succeeds() -> None:
    repo, adapter = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    pdp = FakePDP()
    app = _build_app(repo=repo, pdp=pdp)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))

    with TestClient(app) as client:
        bare = client.delete(RECORD_PATH)
        assert bare.status_code == 428

        ok = client.delete(RECORD_PATH, headers={"if-match": f'"{etag}"'})
        assert ok.status_code == 204

    assert pdp.calls[-1] == ("delete", RECORD_IRI)
    assert RECORD_IRI not in adapter.graphs


# --- OPTIONS ----------------------------------------------------------------


@pytest.mark.unit
async def test_options_advertises_allow_link_accept_post_and_accept_patch() -> None:
    repo, _ = _make_repo()
    containers = FixedContainerRegistry(container_iris={CONTAINER_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers)
    with TestClient(app) as client:
        response = client.options(CONTAINER_PATH)

    assert response.status_code == 204
    allow = response.headers["allow"]
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
        assert method in allow
    assert response.headers["accept-post"] == ", ".join([TURTLE, JSON_LD, RDF_XML, N_TRIPLES])
    assert response.headers["accept-patch"] == SPARQL_UPDATE
    assert "ldp#DirectContainer" in response.headers["link"]


# --- write events (PUT / POST / DELETE) -------------------------------------


@pytest.mark.unit
async def test_put_new_resource_publishes_record_created() -> None:
    repo, _ = _make_repo()
    bus = EventBus()
    created: list[RecordCreated] = []

    async def on_created(evt: RecordCreated) -> None:
        created.append(evt)

    sub = bus.subscribe(RecordCreated, on_created)
    try:
        app = _build_app(repo=repo, pdp=FakePDP(), event_bus=bus)
        body = f'<{RECORD_IRI}> <{DCT.title}> "new" .'.encode()
        with TestClient(app) as client:
            response = client.put(RECORD_PATH, content=body, headers={"content-type": N_TRIPLES})
    finally:
        sub.unsubscribe()

    assert response.status_code == 201
    assert len(created) == 1
    assert created[0].record_iri == RECORD_IRI
    assert created[0].subject == ALICE
    assert created[0].etag == response.headers["etag"].strip('"')


@pytest.mark.unit
async def test_put_replace_publishes_record_modified() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))
    bus = EventBus()
    modified: list[RecordModified] = []
    created: list[RecordCreated] = []

    async def on_modified(evt: RecordModified) -> None:
        modified.append(evt)

    async def on_created(evt: RecordCreated) -> None:
        created.append(evt)

    s1 = bus.subscribe(RecordModified, on_modified)
    s2 = bus.subscribe(RecordCreated, on_created)
    try:
        app = _build_app(repo=repo, pdp=FakePDP(), event_bus=bus)
        body = f'<{RECORD_IRI}> <{DCT.title}> "updated" .'.encode()
        with TestClient(app) as client:
            response = client.put(
                RECORD_PATH,
                content=body,
                headers={"content-type": N_TRIPLES, "if-match": f'"{etag}"'},
            )
    finally:
        s1.unsubscribe()
        s2.unsubscribe()

    assert response.status_code == 200
    assert len(modified) == 1
    assert created == []


@pytest.mark.unit
async def test_post_publishes_record_created_with_member_iri() -> None:
    repo, _ = _make_repo()
    containers = FixedContainerRegistry(container_iris={CONTAINER_IRI})
    bus = EventBus()
    created: list[RecordCreated] = []

    async def on_created(evt: RecordCreated) -> None:
        created.append(evt)

    sub = bus.subscribe(RecordCreated, on_created)
    try:
        app = _build_app(repo=repo, pdp=FakePDP(), containers=containers, event_bus=bus)
        body = b'<urn:_> <http://purl.org/dc/terms/title> "child" .'
        with TestClient(app) as client:
            response = client.post(
                CONTAINER_PATH,
                content=body,
                headers={"content-type": N_TRIPLES, "slug": "bb1"},
            )
    finally:
        sub.unsubscribe()

    assert response.status_code == 201
    expected_iri = f"{CONTAINER_IRI}/bb1"
    assert len(created) == 1
    assert created[0].record_iri == expected_iri
    assert response.headers["location"] == expected_iri


@pytest.mark.unit
async def test_delete_publishes_record_deleted() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    etag = compute_etag(await repo.get_graph(RECORD_IRI))
    bus = EventBus()
    deleted: list[RecordDeleted] = []

    async def on_deleted(evt: RecordDeleted) -> None:
        deleted.append(evt)

    sub = bus.subscribe(RecordDeleted, on_deleted)
    try:
        app = _build_app(repo=repo, pdp=FakePDP(), event_bus=bus)
        with TestClient(app) as client:
            response = client.delete(RECORD_PATH, headers={"if-match": f'"{etag}"'})
    finally:
        sub.unsubscribe()

    assert response.status_code == 204
    assert len(deleted) == 1
    assert deleted[0].record_iri == RECORD_IRI
    assert deleted[0].subject == ALICE


# --- LDP conformance: advisory headers + Prefer minimisation ----------------

_DATASET_REL = "http://www.w3.org/ns/dcat#dataset"


async def _seed_container_with_member(repo: MetadataRepository, member_iri: str) -> None:
    """A container graph carrying ldp:contains + a typed membership triple."""
    g = Graph()
    s = URIRef(CONTAINER_IRI)
    g.add((s, DCT.title, Literal("Catalogs")))
    g.add((s, RDF.type, LDP.DirectContainer))
    g.add((s, LDP.membershipResource, s))
    g.add((s, LDP.hasMemberRelation, URIRef(_DATASET_REL)))
    g.add((s, LDP.contains, URIRef(member_iri)))
    g.add((s, URIRef(_DATASET_REL), URIRef(member_iri)))
    await repo.put_graph(CONTAINER_IRI, g, subject=ALICE)


async def test_post_to_leaf_returns_405_with_allow_header() -> None:
    """405 MUST carry Allow (RFC 7231); a leaf omits POST."""
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP(), containers=FixedContainerRegistry(set()))
    with TestClient(app) as client:
        r = client.post(
            RECORD_PATH, content="<a:a> <a:b> <a:c> .", headers={"Content-Type": TURTLE}
        )
    assert r.status_code == 405
    allow = r.headers["Allow"]
    assert "GET" in allow and "PUT" in allow
    assert "POST" not in allow


async def test_post_unsupported_media_type_advertises_accept_post() -> None:
    repo, _ = _make_repo()
    app = _build_app(repo=repo, pdp=FakePDP(), containers=FixedContainerRegistry({CONTAINER_IRI}))
    with TestClient(app) as client:
        r = client.post(
            CONTAINER_PATH, content="<a/>", headers={"Content-Type": "application/x-tar"}
        )
    assert r.status_code == 415
    assert r.headers["Accept-Post"]


async def test_patch_unsupported_media_type_advertises_accept_patch() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        r = client.patch(RECORD_PATH, content="x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert r.headers["Accept-Patch"] == SPARQL_UPDATE


async def test_get_container_advertises_vary_prefer() -> None:
    repo, _ = _make_repo()
    await _seed_container_with_member(repo, CONTAINER_IRI + "/d1")
    app = _build_app(repo=repo, pdp=FakePDP(), containers=FixedContainerRegistry({CONTAINER_IRI}))
    with TestClient(app) as client:
        r = client.get(CONTAINER_PATH, headers={"Accept": TURTLE})
    assert r.status_code == 200
    assert r.headers["Vary"] == "Prefer"
    # No Prefer sent → full representation, no Preference-Applied.
    assert "Preference-Applied" not in r.headers


async def test_get_container_prefer_omits_containment_and_membership() -> None:
    repo, _ = _make_repo()
    member = CONTAINER_IRI + "/d1"
    await _seed_container_with_member(repo, member)
    app = _build_app(repo=repo, pdp=FakePDP(), containers=FixedContainerRegistry({CONTAINER_IRI}))
    prefer = (
        'return=representation; omit="http://www.w3.org/ns/ldp#PreferContainment '
        'http://www.w3.org/ns/ldp#PreferMembership"'
    )
    with TestClient(app) as client:
        r = client.get(CONTAINER_PATH, headers={"Accept": TURTLE, "Prefer": prefer})
    assert r.status_code == 200
    assert r.headers["Preference-Applied"] == "return=representation"
    g = Graph()
    g.parse(data=r.text, format="turtle")
    s = URIRef(CONTAINER_IRI)
    # Containment + membership triples are gone…
    assert (s, LDP.contains, URIRef(member)) not in g
    assert (s, URIRef(_DATASET_REL), URIRef(member)) not in g
    # …but the minimal container (title + membership config) survives.
    assert (s, DCT.title, Literal("Catalogs")) in g
    assert (s, LDP.hasMemberRelation, URIRef(_DATASET_REL)) in g


async def test_get_container_prefer_minimal_container_omits_both() -> None:
    repo, _ = _make_repo()
    member = CONTAINER_IRI + "/d1"
    await _seed_container_with_member(repo, member)
    app = _build_app(repo=repo, pdp=FakePDP(), containers=FixedContainerRegistry({CONTAINER_IRI}))
    prefer = 'return=representation; include="http://www.w3.org/ns/ldp#PreferMinimalContainer"'
    with TestClient(app) as client:
        r = client.get(CONTAINER_PATH, headers={"Accept": TURTLE, "Prefer": prefer})
    assert r.status_code == 200
    assert r.headers["Preference-Applied"] == "return=representation"
    g = Graph()
    g.parse(data=r.text, format="turtle")
    s = URIRef(CONTAINER_IRI)
    assert (s, LDP.contains, URIRef(member)) not in g
    assert (s, URIRef(_DATASET_REL), URIRef(member)) not in g


# --- ADR-0019: self-describing conformance binding on write -----------------

_SCHEMA_IRI = "http://testserver/fdp-api/schemas/catalog"
_PROFILE_IRI = "http://testserver/fdp-api/profiles/catalog"
_PROFILE_V1 = "http://testserver/fdp-api/profiles/catalog/1"


async def test_put_stamps_conformsto_and_records_validated_against() -> None:
    from fdpneo_server.metadata.prof import provision_profile
    from fdpneo_server.shared.namespaces import FDP_VALIDATED_AGAINST

    repo, adapter = _make_repo()
    await provision_profile(adapter, base_url="http://testserver", slug="catalog", version=1)  # type: ignore[arg-type]
    containers = FixedContainerRegistry(set(), resource_shapes={RECORD_IRI: _SCHEMA_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers, triplestore=adapter)

    body = f'<{RECORD_IRI}> <{DCT.title}> "C" .'
    with TestClient(app) as client:
        r = client.put(RECORD_PATH, content=body, headers={"Content-Type": TURTLE})
    assert r.status_code == 201

    record = adapter.graphs[RECORD_IRI]
    assert (URIRef(RECORD_IRI), DCT.conformsTo, URIRef(_PROFILE_IRI)) in record
    meta = adapter.graphs[RECORD_IRI + "/meta"]
    assert (URIRef(RECORD_IRI), FDP_VALIDATED_AGAINST, URIRef(_PROFILE_V1)) in meta


async def test_put_strips_client_profile_conformsto_keeps_external() -> None:
    from fdpneo_server.metadata.prof import provision_profile

    repo, adapter = _make_repo()
    await provision_profile(adapter, base_url="http://testserver", slug="catalog", version=1)  # type: ignore[arg-type]
    containers = FixedContainerRegistry(set(), resource_shapes={RECORD_IRI: _SCHEMA_IRI})
    app = _build_app(repo=repo, pdp=FakePDP(), containers=containers, triplestore=adapter)

    bogus_profile = "http://testserver/fdp-api/profiles/dataset"  # wrong managed profile
    external = "http://external.example/profile"  # a non-managed conformsTo the client may keep
    body = (
        f'<{RECORD_IRI}> <{DCT.title}> "C" ; <{DCT.conformsTo}> <{bogus_profile}>, <{external}> .'
    )
    with TestClient(app) as client:
        r = client.put(RECORD_PATH, content=body, headers={"Content-Type": TURTLE})
    assert r.status_code == 201

    conforms = set(adapter.graphs[RECORD_IRI].objects(URIRef(RECORD_IRI), DCT.conformsTo))
    # Server profile replaces the client's managed-namespace one; external stays.
    assert conforms == {URIRef(_PROFILE_IRI), URIRef(external)}
