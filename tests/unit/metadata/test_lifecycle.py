"""Unit tests for metadata publication lifecycle (Phase 12, ADR-0010).

Covers the state vocabulary + transition table (``states``), the meta builder's
state defaulting/preservation (``meta``), and the ``lifecycle`` module's reader,
visibility gate, transition service, and router. The fake triple-store adapter
is a real rdflib ``Dataset`` so the SPARQL the reader/service emit (GRAPH,
SELECT/ASK/CONSTRUCT, STRENDS filter) is actually executed, not regex-faked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Dataset, Graph, Literal, URIRef

from fdpneo_server.identity.deps import current_context
from fdpneo_server.metadata.lifecycle import (
    StateGate,
    StateReader,
    StateService,
    build_state_router,
)
from fdpneo_server.metadata.meta import build_meta_graph
from fdpneo_server.metadata.states import (
    DEFAULT_STATE,
    SEED_STATE,
    MetadataState,
    allowed_transitions,
    is_visible_state,
    transition_requires_admin,
)
from fdpneo_server.policy.model import Action, Decision, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import Conflict, Forbidden, NotFound, register_exception_handlers
from fdpneo_server.shared.graphs import meta_graph_uri, record_graph_uri
from fdpneo_server.shared.namespaces import DCT, FDP_METADATA_STATE, OWL, PROV

# The rdflib ``Dataset`` SPARQL path emits internal DeprecationWarnings; this
# is a test-only fake, so silence them rather than let the suite's
# warnings-as-errors policy fail the adapter calls.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BASE = "http://localhost:8000"
REC = f"{BASE}/catalog/x"
NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


# --- fakes -----------------------------------------------------------------


class _DatasetAdapter:
    """Triple-store adapter backed by a real rdflib ``Dataset``."""

    def __init__(self) -> None:
        self.ds = Dataset()

    async def query(self, sparql: str, *, accept: str = "", **_: Any) -> bytes:
        res = self.ds.query(sparql)
        fmt = "turtle" if res.type == "CONSTRUCT" else "json"
        out = res.serialize(format=fmt)
        return out if isinstance(out, bytes) else (out or "").encode("utf-8")

    async def ask(self, sparql: str) -> bool:
        return bool(self.ds.query(sparql).askAnswer)

    async def replace_graph(
        self, graph_uri: str, body: bytes | str, *, mime: str = "application/n-triples"
    ) -> None:
        ctx = self.ds.get_context(URIRef(graph_uri))
        for triple in list(ctx):
            ctx.remove(triple)
        data = body.decode("utf-8") if isinstance(body, bytes) else body
        ctx.parse(data=data, format="nt" if "n-triples" in mime else "turtle")

    # test helper: seed a record's meta graph with a full, valid-ish meta set
    def seed(self, record_iri: str, state: MetadataState) -> None:
        subj = record_graph_uri(record_iri)
        g = self.ds.get_context(meta_graph_uri(record_iri))
        g.add((subj, PROV.wasGeneratedBy, URIRef(record_iri + "#act")))
        g.add((subj, DCT.created, Literal(NOW)))
        g.add((subj, DCT.modified, Literal(NOW)))
        g.add((subj, OWL.versionInfo, Literal(1)))
        g.add((subj, FDP_METADATA_STATE, Literal(state.value)))


class _FakePDP:
    """Minimal PDP: configurable modify-permit set + read/modify graph sets."""

    def __init__(
        self,
        *,
        modify_permit: set[str] | None = None,
        read_graphs: set[str] | None = None,
        modify_graphs: set[str] | None = None,
    ) -> None:
        self._modify_permit = modify_permit or set()
        self._read = read_graphs or set()
        self._modify = modify_graphs or set()

    async def authorize(self, ctx: RequestContext, action: Action, resource_iri: str) -> Decision:
        del ctx
        permit = action is not Action.MODIFY or resource_iri in self._modify_permit
        return Decision(outcome=Outcome.PERMIT if permit else Outcome.DENY, rule=None, reason="")

    async def authorized_graphs(self, ctx: RequestContext, action: Action) -> set[str]:
        del ctx
        return set(self._read if action is Action.READ else self._modify)


def _ctx(
    *, subject: str | None = "https://idp/alice", roles: frozenset[str] = frozenset()
) -> RequestContext:
    return RequestContext(subject=subject, roles=roles, trace_id="t", request_timestamp=NOW)


_ANON = RequestContext.anonymous(trace_id="t", request_timestamp=NOW)


# --- states vocabulary -----------------------------------------------------


@pytest.mark.unit
def test_defaults_and_visibility() -> None:
    assert DEFAULT_STATE is MetadataState.DRAFT
    assert SEED_STATE is MetadataState.PUBLISHED
    assert is_visible_state(MetadataState.PUBLISHED) is True
    assert is_visible_state(MetadataState.DRAFT) is False
    assert is_visible_state(None) is False


@pytest.mark.unit
def test_transition_table() -> None:
    st = MetadataState
    assert transition_requires_admin(st.DRAFT, st.PUBLISHED) is False
    assert transition_requires_admin(st.PUBLISHED, st.DRAFT) is False  # unpublish
    assert transition_requires_admin(st.PUBLISHED, st.ARCHIVED) is False
    assert transition_requires_admin(st.ARCHIVED, st.DRAFT) is True  # admin-only
    # not in the machine
    assert transition_requires_admin(st.DRAFT, st.ARCHIVED) is None
    assert transition_requires_admin(st.ARCHIVED, st.PUBLISHED) is None


@pytest.mark.unit
def test_allowed_transitions_derives_successors_from_the_machine() -> None:
    st = MetadataState
    # Successors match the transition table exactly (admin-only ARCHIVED→DRAFT
    # is still reachable — the grant is a per-request concern, ADR-0022 §3).
    assert allowed_transitions(st.DRAFT) == (st.PUBLISHED,)
    assert allowed_transitions(st.PUBLISHED) == (st.DRAFT, st.ARCHIVED)
    assert allowed_transitions(st.ARCHIVED) == (st.DRAFT,)


# --- meta builder state ----------------------------------------------------


def _state_of_graph(g: Graph, record_iri: str) -> str | None:
    obj = next(iter(g.objects(record_graph_uri(record_iri), FDP_METADATA_STATE)), None)
    return str(obj) if obj is not None else None


@pytest.mark.unit
def test_meta_new_record_defaults_to_draft() -> None:
    result = build_meta_graph(record_iri=REC, prior=Graph(), subject="u", now=NOW)
    assert result.state is MetadataState.DRAFT
    assert _state_of_graph(result.graph, REC) == "DRAFT"


@pytest.mark.unit
def test_meta_seed_record_uses_initial_state() -> None:
    result = build_meta_graph(
        record_iri=REC, prior=Graph(), subject=None, now=NOW, initial_state=SEED_STATE
    )
    assert result.state is MetadataState.PUBLISHED


@pytest.mark.unit
def test_meta_modify_preserves_prior_state_ignoring_initial() -> None:
    # First create as PUBLISHED, then a content edit must keep PUBLISHED even
    # though the default initial_state is DRAFT.
    created = build_meta_graph(
        record_iri=REC, prior=Graph(), subject="u", now=NOW, initial_state=SEED_STATE
    )
    modified = build_meta_graph(record_iri=REC, prior=created.graph, subject="u", now=NOW)
    assert modified.state is MetadataState.PUBLISHED
    assert _state_of_graph(modified.graph, REC) == "PUBLISHED"


# --- StateReader -----------------------------------------------------------


@pytest.mark.unit
async def test_reader_state_of_and_is_published() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.PUBLISHED)
    reader = StateReader(adapter)  # type: ignore[arg-type]
    assert await reader.state_of(REC) is MetadataState.PUBLISHED
    assert await reader.is_published(REC) is True

    other = f"{BASE}/catalog/y"
    adapter.seed(other, MetadataState.DRAFT)
    assert await reader.state_of(other) is MetadataState.DRAFT
    assert await reader.is_published(other) is False
    assert await reader.state_of(f"{BASE}/missing") is None


@pytest.mark.unit
async def test_reader_published_graphs_scopes_to_meta() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(f"{BASE}/a", MetadataState.PUBLISHED)
    adapter.seed(f"{BASE}/b", MetadataState.DRAFT)
    adapter.seed(f"{BASE}/c", MetadataState.PUBLISHED)
    reader = StateReader(adapter)  # type: ignore[arg-type]
    assert await reader.published_graphs() == {f"{BASE}/a", f"{BASE}/c"}


# --- StateGate -------------------------------------------------------------


@pytest.mark.unit
async def test_gate_published_visible_to_anonymous() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.PUBLISHED)
    gate = StateGate(reader=StateReader(adapter), pdp=_FakePDP())  # type: ignore[arg-type]
    await gate.ensure_visible(_ANON, REC)  # no raise
    assert await gate.is_visible(_ANON, REC) is True


@pytest.mark.unit
async def test_gate_draft_hidden_from_anonymous() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    gate = StateGate(reader=StateReader(adapter), pdp=_FakePDP())  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await gate.ensure_visible(_ANON, REC)
    assert await gate.is_visible(_ANON, REC) is False


@pytest.mark.unit
async def test_gate_draft_visible_to_owner_and_admin() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    owner_pdp = _FakePDP(modify_permit={REC})
    gate = StateGate(reader=StateReader(adapter), pdp=owner_pdp)  # type: ignore[arg-type]
    await gate.ensure_visible(_ctx(), REC)  # owner: modify permitted → visible

    admin_gate = StateGate(reader=StateReader(adapter), pdp=_FakePDP())  # type: ignore[arg-type]
    await admin_gate.ensure_visible(_ctx(roles=frozenset({"admin"})), REC)


@pytest.mark.unit
async def test_gate_visible_read_graphs_anonymous_only_published() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(f"{BASE}/pub", MetadataState.PUBLISHED)
    adapter.seed(f"{BASE}/draft", MetadataState.DRAFT)
    pdp = _FakePDP(read_graphs={f"{BASE}/pub", f"{BASE}/draft"})
    gate = StateGate(reader=StateReader(adapter), pdp=pdp)  # type: ignore[arg-type]
    assert await gate.visible_read_graphs(_ANON) == {f"{BASE}/pub"}


@pytest.mark.unit
async def test_gate_visible_read_graphs_owner_sees_own_draft() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(f"{BASE}/pub", MetadataState.PUBLISHED)
    adapter.seed(f"{BASE}/draft", MetadataState.DRAFT)
    pdp = _FakePDP(
        read_graphs={f"{BASE}/pub", f"{BASE}/draft"},
        modify_graphs={f"{BASE}/draft"},
    )
    gate = StateGate(reader=StateReader(adapter), pdp=pdp)  # type: ignore[arg-type]
    assert await gate.visible_read_graphs(_ctx()) == {f"{BASE}/pub", f"{BASE}/draft"}


# --- StateService transitions ----------------------------------------------


def _service(adapter: _DatasetAdapter, pdp: _FakePDP) -> StateService:
    return StateService(
        adapter=adapter,  # type: ignore[arg-type]
        reader=StateReader(adapter),  # type: ignore[arg-type]
        pdp=pdp,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


@pytest.mark.unit
async def test_transition_owner_publishes_draft() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    svc = _service(adapter, _FakePDP(modify_permit={REC}))
    result = await svc.transition(REC, to=MetadataState.PUBLISHED, ctx=_ctx())
    assert result.to_state is MetadataState.PUBLISHED
    # state persisted
    assert await StateReader(adapter).state_of(REC) is MetadataState.PUBLISHED  # type: ignore[arg-type]


@pytest.mark.unit
async def test_transition_stranger_forbidden() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    svc = _service(adapter, _FakePDP())  # no modify permit, not admin
    with pytest.raises(Forbidden):
        await svc.transition(REC, to=MetadataState.PUBLISHED, ctx=_ctx())


@pytest.mark.unit
async def test_transition_archive_to_draft_requires_admin() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.ARCHIVED)
    # owner (modify) is NOT enough for the admin-only transition
    owner = _service(adapter, _FakePDP(modify_permit={REC}))
    with pytest.raises(Forbidden):
        await owner.transition(REC, to=MetadataState.DRAFT, ctx=_ctx())
    # admin succeeds
    admin = _service(adapter, _FakePDP())
    result = await admin.transition(
        REC, to=MetadataState.DRAFT, ctx=_ctx(roles=frozenset({"admin"}))
    )
    assert result.to_state is MetadataState.DRAFT


@pytest.mark.unit
async def test_transition_disallowed_and_noop_conflict() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    svc = _service(adapter, _FakePDP(modify_permit={REC}))
    # DRAFT -> ARCHIVED is not in the machine
    with pytest.raises(Conflict):
        await svc.transition(REC, to=MetadataState.ARCHIVED, ctx=_ctx())
    # no-op DRAFT -> DRAFT
    with pytest.raises(Conflict):
        await svc.transition(REC, to=MetadataState.DRAFT, ctx=_ctx())


@pytest.mark.unit
async def test_transition_missing_record_not_found() -> None:
    adapter = _DatasetAdapter()
    svc = _service(adapter, _FakePDP())
    with pytest.raises(NotFound):
        await svc.transition(f"{BASE}/nope", to=MetadataState.PUBLISHED, ctx=_ctx())


@pytest.mark.unit
async def test_transition_preserves_other_meta_fields() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    svc = _service(adapter, _FakePDP(modify_permit={REC}))
    await svc.transition(REC, to=MetadataState.PUBLISHED, ctx=_ctx())
    meta = adapter.ds.get_context(meta_graph_uri(REC))
    subj = record_graph_uri(REC)
    # version + created untouched; state swapped; modified bumped to NOW
    assert next(iter(meta.objects(subj, OWL.versionInfo))) == Literal(1)
    assert next(iter(meta.objects(subj, FDP_METADATA_STATE))) == Literal("PUBLISHED")
    assert (subj, PROV.wasGeneratedBy, URIRef(REC + "#act")) in meta


# --- router ----------------------------------------------------------------


def _build_client(svc: StateService, *, ctx: RequestContext) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_state_router(service=svc, base_url=BASE))
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


@pytest.mark.unit
def test_router_anonymous_rejected() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    client = _build_client(_service(adapter, _FakePDP(modify_permit={REC})), ctx=_ANON)
    resp = client.post("/catalog/x/state", json={"to": "PUBLISHED"})
    assert resp.status_code == 401


@pytest.mark.unit
def test_router_owner_publishes() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    client = _build_client(_service(adapter, _FakePDP(modify_permit={REC})), ctx=_ctx())
    resp = client.post("/catalog/x/state", json={"to": "PUBLISHED"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_state"] == "DRAFT"
    assert body["to_state"] == "PUBLISHED"
    assert body["record"] == REC


@pytest.mark.unit
def test_router_root_state_path() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(BASE, MetadataState.DRAFT)  # root record graph is base (no slash)
    client = _build_client(
        _service(adapter, _FakePDP(modify_permit={BASE})),
        ctx=_ctx(roles=frozenset({"admin"})),
    )
    resp = client.post("/state", json={"to": "PUBLISHED"})
    assert resp.status_code == 200
    assert resp.json()["record"] == BASE


@pytest.mark.unit
def test_router_rejects_unknown_state_value() -> None:
    adapter = _DatasetAdapter()
    adapter.seed(REC, MetadataState.DRAFT)
    client = _build_client(_service(adapter, _FakePDP(modify_permit={REC})), ctx=_ctx())
    resp = client.post("/catalog/x/state", json={"to": "BOGUS"})
    assert resp.status_code == 422


@pytest.mark.unit
def test_router_managed_resource_resolves_under_reserved_prefix() -> None:
    """A policy/license lives at <base>/fdp-api/…; the state router must target it.

    Mounted under /fdp-api in the app, the handler receives the prefix-stripped
    sub-path (``policies/p1``); ``state_record_iri`` re-adds the reserved prefix
    so the transition hits the stored graph rather than a non-existent root IRI.
    """
    managed = f"{BASE}/fdp-api/policies/p1"
    adapter = _DatasetAdapter()
    adapter.seed(managed, MetadataState.DRAFT)
    client = _build_client(
        _service(adapter, _FakePDP(modify_permit={managed})),
        ctx=_ctx(roles=frozenset({"admin"})),
    )
    resp = client.post("/policies/p1/state", json={"to": "PUBLISHED"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["record"] == managed
