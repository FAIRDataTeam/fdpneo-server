"""Unit tests for the profile applier."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl, PostgresDsn
from rdflib import Graph

from fdpneo_server.config import OIDCSettings, Settings, TripleStoreSettings
from fdpneo_server.metadata.profiles import apply_profile, load_profile, resolve_runtime_state
from fdpneo_server.metadata.profiles.applier import _repository_graph, _service_advertisement
from fdpneo_server.shared.errors import BadRequest, Conflict
from fdpneo_server.shared.namespaces import DCAT, VOID

# --- in-memory fakes -------------------------------------------------------


@dataclass
class _FakeRepo:
    put_calls: list[tuple[str, int]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    fail_on_put: str | None = None
    """If set to an IRI, the put_graph call for that IRI raises."""

    async def put_graph(
        self, record_uri: str, graph: Graph, *, subject: str | None, initial_state: object = None
    ) -> str:
        del subject, initial_state
        if self.fail_on_put is not None and record_uri == self.fail_on_put:
            raise RuntimeError("simulated triple-store failure")
        self.put_calls.append((record_uri, len(graph)))
        return "etag-" + str(len(self.put_calls))

    async def delete_graph(self, record_uri: str) -> None:
        self.delete_calls.append(record_uri)


@dataclass
class _FakeState:
    applied: bool = False
    recorded: dict[str, str] | None = None
    cleared: int = 0

    async def current(self) -> Any:
        return object() if self.applied else None

    async def is_applied(self) -> bool:
        return self.applied

    async def record(
        self,
        *,
        name: str,
        version: str,
        manifest_checksum: str,
        applied_at: Any = None,
    ) -> Any:
        del applied_at
        self.recorded = {
            "name": name,
            "version": version,
            "manifest_checksum": manifest_checksum,
        }
        return object()

    async def clear(self) -> int:
        self.cleared += 1
        return 1


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _settings() -> Settings:
    return Settings(
        postgres_dsn=PostgresDsn("postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test"),
        triplestore=TripleStoreSettings(
            query_endpoint=HttpUrl("http://triplestore.local/query"),
            update_endpoint=HttpUrl("http://triplestore.local/update"),
        ),
        oidc=OIDCSettings(
            issuer=HttpUrl("http://idp.local/realms/fdp"),
            audience="fdp",
        ),
    )


# --- happy path ----------------------------------------------------------


@pytest.mark.unit
async def test_apply_writes_schemas_then_offers_then_repository_seed(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    repo = _FakeRepo()
    state = _FakeState()
    session = _FakeSession()

    report = await apply_profile(
        profile,
        repository=repo,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        settings=_settings(),
    )

    iris = [c[0] for c in repo.put_calls]
    # Apply order: schemas → offers → license shape → default licenses → RD shape
    # → RD records → Repository seed (ADR-0009 / ADR-0012). The offer is rewritten
    # to its deployment-local managed-policy IRI ({base}/fdp-api/policies/{id}); the
    # server-owned license SHACL shape is seeded so PUT /licenses can validate,
    # then the built-in default license set lands at {base}/fdp-api/licenses/{id}. The
    # single resource definition is the root Repository; its record lands under
    # the reserved resource-definitions namespace, slugged from its name. The
    # Repository seed itself lives at the configured base_url (the API root) so
    # the LDP layer serves it at "/".
    assert iris == [
        "http://localhost:8000/fdp-api/schemas/catalog",
        "http://localhost:8000/fdp-api/policies/system-default",
        "urn:fdp-shape:license-document",
        "http://localhost:8000/fdp-api/licenses/cc0-1.0",
        "http://localhost:8000/fdp-api/licenses/cc-by-4.0",
        "http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0",
        "urn:fdp-shape:resource-definition",
        "http://localhost:8000/fdp-api/resource-definitions/repository",
        "http://localhost:8000",
    ]
    assert report.total_written == 9
    assert report.license_shape_iri == "urn:fdp-shape:license-document"
    assert report.offers_written == ["http://localhost:8000/fdp-api/policies/system-default"]
    assert report.licenses_written == [
        "http://localhost:8000/fdp-api/licenses/cc0-1.0",
        "http://localhost:8000/fdp-api/licenses/cc-by-4.0",
        "http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0",
    ]
    assert report.rd_shape_iri == "urn:fdp-shape:resource-definition"
    assert report.resource_definition_records == [
        "http://localhost:8000/fdp-api/resource-definitions/repository"
    ]
    assert report.repository_iri == "http://localhost:8000"
    assert report.rolled_back is False
    assert state.recorded is not None
    assert state.recorded["name"] == "test"
    assert session.committed is True

    # The cache from build_cache_from_manifest is handed to the caller
    # so app.state.resource_definitions can be populated post-apply.
    assert report.resource_definitions is not None
    assert report.resource_definitions.root() is not None


_MANIFEST_WITH_META = """\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata:
  name: test
  version: 0.1.0
schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl
metaMetadataSchema:
  path: schemas/meta-metadata.ttl
offers:
  - id: system-default
    path: offers/system-default.ttl
    isSystemDefault: true
resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: dcat:Catalog
"""

_META_SHAPE_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
<urn:fdp-shape:meta-metadata>
    a sh:NodeShape ; sh:targetClass prov:Entity .
"""


@pytest.mark.unit
async def test_apply_stores_meta_shape_first_so_runtime_can_validate(
    write_bundle: Callable[..., Path],
) -> None:
    bundle = write_bundle(
        manifest_text=_MANIFEST_WITH_META,
        extra_files={"schemas/meta-metadata.ttl": _META_SHAPE_TTL},
    )
    profile = load_profile(bundle)
    repo = _FakeRepo()
    report = await apply_profile(
        profile,
        repository=repo,  # type: ignore[arg-type]
        state=_FakeState(),  # type: ignore[arg-type]
        session=_FakeSession(),  # type: ignore[arg-type]
        settings=_settings(),
    )
    iris = [c[0] for c in repo.put_calls]
    # The meta shape is written first, at its fixed IRI, so the very next meta
    # refresh (and all runtime writes) can validate against it.
    assert iris[0] == "urn:fdp-shape:meta-metadata"
    assert report.meta_shape_iri == "urn:fdp-shape:meta-metadata"


# --- already-initialized refusal ----------------------------------------


@pytest.mark.unit
def test_resolve_runtime_state_derives_offer_and_definitions_without_writes(
    write_bundle: Callable[..., Path],
) -> None:
    # Regression: on restart the profile is already applied, so apply_profile is
    # skipped. The runtime state (system-default offer IRI + resource-definition
    # cache) must still be derivable from the profile alone — otherwise the
    # offer-resolver fallback is unset and creating new records is default-denied.
    profile = load_profile(write_bundle())

    system_default_offer_iri, resource_definitions = resolve_runtime_state(
        profile, settings=_settings()
    )

    assert system_default_offer_iri == "http://localhost:8000/fdp-api/policies/system-default"
    assert resource_definitions is not None
    assert resource_definitions.root() is not None


@pytest.mark.unit
async def test_apply_refuses_when_already_initialized(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    state = _FakeState(applied=True)
    with pytest.raises(Conflict):
        await apply_profile(
            profile,
            repository=_FakeRepo(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            session=_FakeSession(),  # type: ignore[arg-type]
            settings=_settings(),
        )


@pytest.mark.unit
async def test_apply_force_skips_the_already_initialized_check(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    state = _FakeState(applied=True)
    # Force=True bypasses the refusal but the caller (CLI) is expected
    # to have cleared state already. Here we just confirm the applier
    # itself doesn't raise on force.
    state.applied = False  # simulate post-clear
    report = await apply_profile(
        profile,
        repository=_FakeRepo(),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        session=_FakeSession(),  # type: ignore[arg-type]
        settings=_settings(),
        force=True,
    )
    assert report.rolled_back is False


# --- rollback on failure --------------------------------------------------


@pytest.mark.unit
async def test_apply_rolls_back_on_triple_store_failure(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    # Fail on the Repository seed (the last put). Schema, offer, RD shape
    # and the RD record were already written, so all must be dropped
    # during rollback.
    repo = _FakeRepo(fail_on_put="http://localhost:8000")
    state = _FakeState()
    session = _FakeSession()

    with pytest.raises(Exception) as exc:  # ApplyError or pass-through
        await apply_profile(
            profile,
            repository=repo,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
            settings=_settings(),
        )

    # All prior writes (schema, managed-policy offer, license shape, default
    # licenses, RD shape, RD record) were rolled back in reverse order. The
    # Repository seed itself never succeeded so isn't dropped.
    assert repo.delete_calls == [
        "http://localhost:8000/fdp-api/resource-definitions/repository",
        "urn:fdp-shape:resource-definition",
        "http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0",
        "http://localhost:8000/fdp-api/licenses/cc-by-4.0",
        "http://localhost:8000/fdp-api/licenses/cc0-1.0",
        "urn:fdp-shape:license-document",
        "http://localhost:8000/fdp-api/policies/system-default",
        "http://localhost:8000/fdp-api/schemas/catalog",
    ]
    assert session.rolled_back is True
    assert state.recorded is None
    assert "profile_apply" in repr(exc.value) or "simulated" in repr(exc.value)


# --- validation failure rejects before any writes ------------------------


@pytest.mark.unit
async def test_apply_refuses_invalid_profile_without_writing(
    write_bundle: Callable[..., Path],
) -> None:
    # Resource definition declares a schema that wasn't listed in
    # schemas[] → validator's rd_schema_not_declared fires before any
    # write hits the triple store.
    from tests.unit.metadata.profiles.conftest import MANIFEST

    bad_manifest = MANIFEST.replace("schema: dcat:Catalog", "schema: dcat:Unknown")
    profile = load_profile(write_bundle(manifest_text=bad_manifest))
    repo = _FakeRepo()
    state = _FakeState()

    with pytest.raises(BadRequest):
        await apply_profile(
            profile,
            repository=repo,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            session=_FakeSession(),  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert repo.put_calls == []


# --- Direct Container membership config (task 15.1) ------------------------


@pytest.mark.unit
def test_direct_container_config_emits_membership_triad() -> None:
    from rdflib import RDF, URIRef

    from fdpneo_server.metadata.profiles.applier import direct_container_config
    from fdpneo_server.shared.namespaces import LDP

    subject = URIRef("http://localhost:8000")
    catalog = "http://www.w3.org/ns/dcat#catalog"
    triples = direct_container_config(subject, [catalog])
    assert (subject, RDF.type, LDP.DirectContainer) in triples
    assert (subject, LDP.membershipResource, subject) in triples
    assert (subject, LDP.insertedContentRelation, LDP.MemberSubject) in triples
    assert (subject, LDP.hasMemberRelation, URIRef(catalog)) in triples
    # Basic Container is gone — the root is a Direct Container now.
    assert (subject, RDF.type, LDP.BasicContainer) not in triples


def test_service_advertisement_emits_sparql_and_search_endpoints() -> None:
    from rdflib import RDF, URIRef

    root = URIRef("http://localhost:8000")
    triples = _service_advertisement(root, "http://localhost:8000", search_enabled=True)
    sparql = URIRef("http://localhost:8000/fdp-api/sparql")
    search = URIRef("http://localhost:8000/fdp-api/search")
    # Direct VoID discovery signal for SPARQL.
    assert (root, VOID.sparqlEndpoint, sparql) in triples
    # DCAT DataService descriptors with endpointURL for both endpoints.
    endpoint_urls = {o for _, p, o in triples if p == DCAT.endpointURL}
    assert endpoint_urls == {sparql, search}
    services = [s for s, p, o in triples if p == RDF.type and o == DCAT.DataService]
    assert len(services) == 2
    assert all((root, DCAT.service, svc) in triples for svc in services)


def test_service_advertisement_omits_search_when_disabled() -> None:
    from rdflib import URIRef

    root = URIRef("http://localhost:8000")
    triples = _service_advertisement(root, "http://localhost:8000", search_enabled=False)
    endpoint_urls = {str(o) for _, p, o in triples if p == DCAT.endpointURL}
    assert endpoint_urls == {"http://localhost:8000/fdp-api/sparql"}  # no search
    assert (root, VOID.sparqlEndpoint, URIRef("http://localhost:8000/fdp-api/sparql")) in triples


def test_repository_graph_dual_types_a_fairdatapoint_root() -> None:
    # FDP Index validators (home.fairdatapoint.org) match fdp-o:MetadataService
    # literally, no subclass inference — the root must assert both types.
    from rdflib import RDF, URIRef

    from fdpneo_server.metadata.profiles.applier import root_type_iris

    fdp = "https://w3id.org/fdp/fdp-o#FAIRDataPoint"
    service = "https://w3id.org/fdp/fdp-o#MetadataService"
    assert root_type_iris(fdp) == (fdp, service)
    assert root_type_iris("http://www.w3.org/ns/dcat#Catalog") == (
        "http://www.w3.org/ns/dcat#Catalog",
    )

    g = _repository_graph(
        iri="http://localhost:8000",
        type_iri=fdp,
        member_relations=[],
        title="Test FDP",
        rights_iri=None,
    )
    root = URIRef("http://localhost:8000")
    assert (root, RDF.type, URIRef(fdp)) in g
    assert (root, RDF.type, URIRef(service)) in g


def test_repository_graph_includes_service_advertisement() -> None:
    from rdflib import URIRef

    g = _repository_graph(
        iri="http://localhost:8000",
        type_iri="https://w3id.org/fdp/fdp-o#FAIRDataPoint",
        member_relations=["http://www.w3.org/ns/dcat#catalog"],
        title="Test FDP",
        rights_iri=None,
        search_enabled=True,
    )
    root = URIRef("http://localhost:8000")
    assert (root, VOID.sparqlEndpoint, URIRef("http://localhost:8000/fdp-api/sparql")) in g
    assert any(p == DCAT.service for _, p, _ in g)


async def test_ensure_root_service_advertisement_adds_then_idempotent() -> None:
    from rdflib import RDF, URIRef

    from fdpneo_server.metadata.profiles.applier import ensure_root_service_advertisement

    class _Store:
        def __init__(self) -> None:
            self.graphs: dict[str, Graph] = {}

        async def get_graph(self, record_uri: str) -> Graph:
            return self.graphs.get(str(record_uri).rstrip("/"), Graph())

        async def replace_graph(self, graph_uri: str, data: str, *, mime: str = "") -> None:
            del mime
            g = Graph()
            g.parse(data=data, format="nt")
            self.graphs[str(graph_uri).rstrip("/")] = g

    store = _Store()
    root = "http://localhost:8000"
    seed = Graph()
    seed.add((URIRef(root), RDF.type, URIRef("https://w3id.org/fdp/fdp-o#FAIRDataPoint")))
    store.graphs[root] = seed

    added = await ensure_root_service_advertisement(store, store, base_url=root)  # type: ignore[arg-type]
    assert added is True
    assert (URIRef(root), VOID.sparqlEndpoint, URIRef(f"{root}/fdp-api/sparql")) in store.graphs[
        root
    ]
    # Second pass is a no-op (idempotent — never clobbers).
    assert await ensure_root_service_advertisement(store, store, base_url=root) is False  # type: ignore[arg-type]


async def test_ensure_root_service_advertisement_noop_when_no_root() -> None:
    from fdpneo_server.metadata.profiles.applier import ensure_root_service_advertisement

    class _Empty:
        async def get_graph(self, record_uri: str) -> Graph:
            del record_uri
            return Graph()

        async def replace_graph(self, graph_uri: str, data: str, *, mime: str = "") -> None:
            raise AssertionError("must not write when there is no root record")

    assert (
        await ensure_root_service_advertisement(
            _Empty(),  # type: ignore[arg-type]
            _Empty(),  # type: ignore[arg-type]
            base_url="http://localhost:8000",
        )
        is False
    )
