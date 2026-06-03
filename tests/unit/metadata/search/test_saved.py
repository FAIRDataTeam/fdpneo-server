"""Unit tests for saved queries (Phase 7.3) over in-memory SQLite."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.identity.deps import current_context
from fdp.metadata.search.saved import (
    SavedQueryCreate,
    SavedQueryRepository,
    SavedQueryService,
    SavedQueryUpdate,
    build_saved_queries_router,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Forbidden, NotFound, register_exception_handlers
from fdp.storage.postgres.models import Base, register_all_models

ALICE = "https://idp/alice"
BOB = "https://idp/bob"


@pytest.fixture
async def session_factory() -> Any:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _ctx(subject: str | None, *, roles: frozenset[str] = frozenset()) -> RequestContext:
    return RequestContext(subject=subject, roles=roles, trace_id="t")


def _service(session_factory: Any) -> SavedQueryService:
    return SavedQueryService(repository=SavedQueryRepository(session_factory=session_factory))


@pytest.mark.unit
async def test_create_validates_query(session_factory: Any) -> None:
    svc = _service(session_factory)
    ok = await svc.create(_ctx(ALICE), SavedQueryCreate(name="mine", query={"query": "dna"}))
    assert ok.mine is True
    assert ok.shared is False
    with pytest.raises(BadRequest):
        await svc.create(_ctx(ALICE), SavedQueryCreate(name="bad", query={"limit": -3}))


@pytest.mark.unit
async def test_list_returns_own_plus_shared(session_factory: Any) -> None:
    svc = _service(session_factory)
    mine = await svc.create(_ctx(ALICE), SavedQueryCreate(name="a", query={}))
    bobs = await svc.create(_ctx(BOB), SavedQueryCreate(name="b", query={}))
    # Admin shares Bob's query.
    await svc.update(_ctx(BOB, roles=frozenset({"admin"})), bobs.id, SavedQueryUpdate(shared=True))

    visible = {q.id for q in await svc.list_for(_ctx(ALICE))}
    assert mine.id in visible
    assert bobs.id in visible  # shared
    # Alice does not see Bob's *unshared* query.
    bob_private = await svc.create(_ctx(BOB), SavedQueryCreate(name="c", query={}))
    assert bob_private.id not in {q.id for q in await svc.list_for(_ctx(ALICE))}


@pytest.mark.unit
async def test_shared_toggle_is_admin_only(session_factory: Any) -> None:
    svc = _service(session_factory)
    mine = await svc.create(_ctx(ALICE), SavedQueryCreate(name="a", query={}))
    # Owner cannot self-publish.
    with pytest.raises(Forbidden):
        await svc.update(_ctx(ALICE), mine.id, SavedQueryUpdate(shared=True))
    # Admin can.
    updated = await svc.update(
        _ctx(ALICE, roles=frozenset({"admin"})), mine.id, SavedQueryUpdate(shared=True)
    )
    assert updated.shared is True


@pytest.mark.unit
async def test_update_and_delete_ownership(session_factory: Any) -> None:
    svc = _service(session_factory)
    mine = await svc.create(_ctx(ALICE), SavedQueryCreate(name="a", query={}))
    # Stranger cannot update or delete.
    with pytest.raises(Forbidden):
        await svc.update(_ctx(BOB), mine.id, SavedQueryUpdate(name="x"))
    with pytest.raises(Forbidden):
        await svc.delete(_ctx(BOB), mine.id)
    # Owner renames; admin deletes anyone's.
    renamed = await svc.update(_ctx(ALICE), mine.id, SavedQueryUpdate(name="renamed"))
    assert renamed.name == "renamed"
    await svc.delete(_ctx(BOB, roles=frozenset({"admin"})), mine.id)
    with pytest.raises(NotFound):
        await svc.delete(_ctx(ALICE), mine.id)


# --- router ----------------------------------------------------------------


def _client(session_factory: Any, *, ctx: RequestContext) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_saved_queries_router(service=_service(session_factory)))
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


@pytest.mark.unit
def test_router_anonymous_rejected(session_factory: Any) -> None:
    client = _client(session_factory, ctx=RequestContext.anonymous(trace_id="t"))
    assert client.get("/me/saved-queries").status_code == 401
    assert client.post("/me/saved-queries", json={"name": "x", "query": {}}).status_code == 401


@pytest.mark.unit
def test_router_create_list_flow(session_factory: Any) -> None:
    client = _client(session_factory, ctx=_ctx(ALICE))
    created = client.post("/me/saved-queries", json={"name": "mine", "query": {"query": "x"}})
    assert created.status_code == 201, created.text
    assert client.get("/me/saved-queries").json()["queries"][0]["name"] == "mine"
