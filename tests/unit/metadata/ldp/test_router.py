"""Unit tests for the LDP router skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef

from fdp.identity.deps import current_context
from fdp.metadata.etag import compute_etag
from fdp.metadata.graphs import meta_graph_uri
from fdp.metadata.ldp import ContainerRegistry, build_ldp_router
from fdp.metadata.ldp.negotiation import (
    JSON_LD,
    N_TRIPLES,
    RDF_XML,
    SPARQL_UPDATE,
    TURTLE,
)
from fdp.metadata.repository import MetadataRepository
from fdp.metadata.shacl import InMemoryShapeProvider, ShaclValidator
from fdp.policy.model import Action, Decision, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import register_exception_handlers
from fdp.shared.namespaces import DCT

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

    async def authorize(
        self, ctx: RequestContext, action: Action, resource_iri: str
    ) -> Decision:
        del ctx
        self.calls.append((action.value, resource_iri))
        outcome = self.overrides.get((action.value, resource_iri), self.default)
        return Decision(outcome=outcome, rule=None, reason="test")


class FixedContainerRegistry:
    """Container registry that flags a fixed set of IRIs and shape bindings."""

    def __init__(
        self,
        container_iris: set[str],
        shape_for: dict[str, str] | None = None,
    ) -> None:
        self._containers = set(container_iris)
        self._shapes = dict(shape_for or {})

    def is_container(self, resource_iri: str) -> bool:
        return resource_iri in self._containers

    def member_shape(self, container_iri: str) -> str | None:
        return self._shapes.get(container_iri)


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
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        build_ldp_router(
            repo=repo,  # type: ignore[arg-type]
            pdp=pdp,  # type: ignore[arg-type]
            validator=validator,
            containers=containers,
        )
    )
    app.dependency_overrides[current_context] = lambda: ctx or _authenticated_ctx()
    return app


async def _seed_record(repo: MetadataRepository, iri: str, *, title: str = "hello") -> str:
    graph = Graph()
    graph.add((URIRef(iri), DCT.title, Literal(title)))
    return await repo.put_graph(iri, graph, creator=ALICE)


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


# --- PUT --------------------------------------------------------------------


@pytest.mark.unit
async def test_put_new_resource_returns_201_with_location() -> None:
    repo, _ = _make_repo()
    app = _build_app(repo=repo, pdp=FakePDP())
    body = f'<{RECORD_IRI}> <{DCT.title}> "new" .'.encode()
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH, content=body, headers={"content-type": N_TRIPLES}
        )

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
        response = client.put(
            RECORD_PATH, content=body, headers={"content-type": N_TRIPLES}
        )

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
async def test_put_invalid_against_member_shape_returns_422() -> None:
    repo, _ = _make_repo()
    validator = ShaclValidator(InMemoryShapeProvider({DATASET_SHAPE_IRI: DATASET_SHAPE_TTL}))
    containers = FixedContainerRegistry(
        container_iris={RECORD_IRI},  # treat the target as a container for shape lookup
        shape_for={RECORD_IRI: DATASET_SHAPE_IRI},
    )
    app = _build_app(repo=repo, pdp=FakePDP(), validator=validator, containers=containers)
    # Body declares a dcat:Dataset but omits the required dct:title.
    body = (
        f"<{RECORD_IRI}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://www.w3.org/ns/dcat#Dataset> ."
    ).encode()
    with TestClient(app) as client:
        response = client.put(
            RECORD_PATH, content=body, headers={"content-type": N_TRIPLES}
        )

    assert response.status_code == 422
    assert response.json()["code"] == "fdp.schema_violation"


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
        response = client.post(
            CONTAINER_PATH, content=body, headers={"content-type": N_TRIPLES}
        )

    assert response.status_code == 201
    location = response.headers["location"]
    assert location.startswith(CONTAINER_IRI + "/")
    # The mint-by-uuid path is 36 hex/dashes.
    assert len(location[len(CONTAINER_IRI) + 1 :]) == 36


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
async def test_patch_runs_repository_update_and_returns_204() -> None:
    repo, adapter = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    current_etag = compute_etag(await repo.get_graph(RECORD_IRI))

    app = _build_app(repo=repo, pdp=FakePDP())
    sparql = (
        f"INSERT DATA {{ GRAPH <{RECORD_IRI}> "
        f"{{ <{RECORD_IRI}> <{DCT.description}> \"added\" }} }}"
    )
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH,
            content=sparql.encode("utf-8"),
            headers={"content-type": SPARQL_UPDATE, "if-match": f'"{current_etag}"'},
        )

    assert response.status_code == 204
    assert adapter.update_calls == [sparql]
    # version bumped via meta refresh
    meta = adapter.graphs[str(meta_graph_uri(RECORD_IRI))]
    assert any(p == DCT.modified for _, p, _ in meta)


@pytest.mark.unit
async def test_patch_missing_if_match_returns_428() -> None:
    repo, _ = _make_repo()
    await _seed_record(repo, RECORD_IRI)
    app = _build_app(repo=repo, pdp=FakePDP())
    with TestClient(app) as client:
        response = client.patch(
            RECORD_PATH, content=b"INSERT DATA { <x> <y> <z> }", headers={"content-type": SPARQL_UPDATE}
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
    assert response.headers["accept-post"] == ", ".join(
        [TURTLE, JSON_LD, RDF_XML, N_TRIPLES]
    )
    assert response.headers["accept-patch"] == SPARQL_UPDATE
    assert "ldp#DirectContainer" in response.headers["link"]
