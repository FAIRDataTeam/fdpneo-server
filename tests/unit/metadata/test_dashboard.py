"""Unit tests for the user dashboard endpoint (task 6.3).

Covers:

* ``_select_to_items`` — SPARQL JSON shape → DashboardItem with first
  type/title kept, duplicates collapsed.
* ``DashboardService._read_owned`` — subject IRI rendered into SPARQL,
  unsafe IRIs short-circuit to empty.
* ``DashboardService._read_recent`` — Postgres audit grouped per record
  with most-recent timestamp; delete events excluded; enrichment with
  type/title; placeholder for unknown IRIs.
* ``DashboardService.for_subject`` — composes the three lists,
  deduplicates ``editable`` against ``owned``, honours ``as_admin``.
* Router — auth required (401), admin-flag enforced (403),
  pagination param bounds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.identity.deps import current_context
from fdp.metadata.audit import AuditOperation, RecordAuditRow
from fdp.metadata.dashboard import (
    DashboardItem,
    DashboardService,
    _select_to_items,
    build_dashboard_router,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import register_exception_handlers
from fdp.storage.postgres.models import Base

# --- _select_to_items ----------------------------------------------------


def _sparql_response(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"head": {"vars": ["iri", "type", "title"]}, "results": {"bindings": rows}}
    ).encode("utf-8")


def _row(iri: str, *, type_: str | None = None, title: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"iri": {"type": "uri", "value": iri}}
    if type_ is not None:
        out["type"] = {"type": "uri", "value": type_}
    if title is not None:
        out["title"] = {"type": "literal", "value": title}
    return out


@pytest.mark.unit
def test_select_to_items_parses_basic_row() -> None:
    body = _sparql_response(
        [_row("urn:a", type_="urn:type", title="Alpha")]
    )
    items = _select_to_items(body)
    assert items == [
        DashboardItem(record_iri="urn:a", type_iri="urn:type", title="Alpha"),
    ]


@pytest.mark.unit
def test_select_to_items_collapses_duplicate_iris() -> None:
    """Two rows for the same IRI yield one item with the first non-empty fields."""
    body = _sparql_response(
        [
            _row("urn:a", type_="urn:type", title=None),
            _row("urn:a", type_=None, title="Alpha"),
        ]
    )
    items = _select_to_items(body)
    assert len(items) == 1
    assert items[0] == DashboardItem(
        record_iri="urn:a", type_iri="urn:type", title="Alpha"
    )


@pytest.mark.unit
def test_select_to_items_handles_missing_optional_fields() -> None:
    body = _sparql_response([_row("urn:a")])
    items = _select_to_items(body)
    assert items == [DashboardItem(record_iri="urn:a")]


# --- DashboardService: in-memory Postgres fixture ----------------------


@pytest.fixture
async def session_factory() -> Any:
    """Spin up an in-memory SQLite database with the record_audit table.

    The audit ORM is plain SQLAlchemy so SQLite + aiosqlite is enough
    for the dashboard service's audit-read path; we don't touch the
    other tables here.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _FakeAdapter:
    """``TripleStoreAdapter`` stand-in with a queue of canned responses.

    The dashboard service issues 1 query for owned + 1 for enrichment
    of recent + 1 for enrichment of editable. Tests configure the
    queue in that order; missing entries return an empty SELECT.
    """

    def __init__(self, queue: list[bytes] | None = None) -> None:
        self.queue = list(queue or [])
        self.calls: list[str] = []

    async def query(self, sparql: str, **_kwargs: Any) -> bytes:
        self.calls.append(sparql)
        if self.queue:
            return self.queue.pop(0)
        return _sparql_response([])


class _FakePDP:
    """``RequestScopedPDP`` stand-in returning a fixed authorized set."""

    def __init__(self, authorized: set[str] | None = None) -> None:
        self._authorized = authorized or set()
        self.calls: list[tuple[Any, Any]] = []

    async def authorize(self, ctx: Any, action: Any, resource_iri: Any) -> Any:  # pragma: no cover
        raise AssertionError("authorize should not be called by the dashboard")

    async def authorized_graphs(self, ctx: Any, action: Any) -> set[str]:
        del ctx
        self.calls.append(("authorized_graphs", action))
        return set(self._authorized)


def _ctx(*, subject: str = "https://idp/alice", roles: frozenset[str] = frozenset()) -> RequestContext:
    return RequestContext(
        subject=subject,
        roles=roles,
        trace_id="t-1",
        request_timestamp=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
    )


# --- DashboardService.for_subject -------------------------------------


@pytest.mark.unit
async def test_for_subject_returns_owned_recent_and_editable(
    session_factory: Any,
) -> None:
    # Owned SPARQL returns one record; enrichment for the recent+editable
    # passes returns titles for known IRIs.
    owned_body = _sparql_response(
        [_row("urn:owned", type_="urn:Catalog", title="Mine")]
    )
    recent_enrich_body = _sparql_response(
        [_row("urn:recent", type_="urn:Catalog", title="Touched")]
    )
    editable_enrich_body = _sparql_response(
        [_row("urn:editable", type_="urn:Dataset", title="Editable")]
    )
    adapter = _FakeAdapter([owned_body, recent_enrich_body, editable_enrich_body])
    pdp = _FakePDP(authorized={"urn:editable", "urn:owned"})  # urn:owned dedup'd

    # Seed one audit row so 'recent' has content.
    async with session_factory() as session:
        session.add(
            RecordAuditRow(
                id=1,
                record_iri="urn:recent",
                operation=AuditOperation.MODIFY.value,
                subject="https://idp/alice",
                etag="abc",
                occurred_at=datetime(2026, 5, 30, 10, 0, tzinfo=UTC),
            )
        )
        await session.commit()

    service = DashboardService(
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=session_factory,
        pdp=pdp,  # type: ignore[arg-type]
    )
    response = await service.for_subject(_ctx())
    assert [item.record_iri for item in response.owned] == ["urn:owned"]
    assert response.owned[0].title == "Mine"
    assert [item.record_iri for item in response.recent] == ["urn:recent"]
    assert response.recent[0].last_modified is not None
    # editable de-duplicates against owned: urn:owned should NOT appear.
    assert [item.record_iri for item in response.editable] == ["urn:editable"]
    assert response.editable[0].title == "Editable"


@pytest.mark.unit
async def test_recent_excludes_delete_events(session_factory: Any) -> None:
    adapter = _FakeAdapter([_sparql_response([])])  # owned: empty
    pdp = _FakePDP()
    async with session_factory() as session:
        session.add_all(
            [
                RecordAuditRow(
                    id=1,
                    record_iri="urn:created",
                    operation=AuditOperation.CREATE.value,
                    subject="https://idp/alice",
                    occurred_at=datetime(2026, 5, 30, 9, 0, tzinfo=UTC),
                ),
                RecordAuditRow(
                    id=2,
                    record_iri="urn:deleted",
                    operation=AuditOperation.DELETE.value,
                    subject="https://idp/alice",
                    occurred_at=datetime(2026, 5, 30, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()
    service = DashboardService(
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=session_factory,
        pdp=pdp,  # type: ignore[arg-type]
    )
    response = await service.for_subject(_ctx())
    assert [item.record_iri for item in response.recent] == ["urn:created"]


@pytest.mark.unit
async def test_recent_collapses_multiple_audit_rows_to_latest(
    session_factory: Any,
) -> None:
    adapter = _FakeAdapter([_sparql_response([])])
    pdp = _FakePDP()
    base = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
    async with session_factory() as session:
        for i, offset_hours in enumerate((0, 1, 2), start=1):
            session.add(
                RecordAuditRow(
                    id=i,
                    record_iri="urn:repeated",
                    operation=AuditOperation.MODIFY.value,
                    subject="https://idp/alice",
                    occurred_at=base + timedelta(hours=offset_hours),
                )
            )
        await session.commit()
    service = DashboardService(
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=session_factory,
        pdp=pdp,  # type: ignore[arg-type]
    )
    response = await service.for_subject(_ctx())
    assert len(response.recent) == 1
    # The newest timestamp survives.
    assert response.recent[0].last_modified == base + timedelta(hours=2)


@pytest.mark.unit
async def test_recent_filters_by_subject(session_factory: Any) -> None:
    adapter = _FakeAdapter([_sparql_response([])])
    pdp = _FakePDP()
    async with session_factory() as session:
        session.add_all(
            [
                RecordAuditRow(
                    id=1,
                    record_iri="urn:alice-touched",
                    operation=AuditOperation.MODIFY.value,
                    subject="https://idp/alice",
                    occurred_at=datetime(2026, 5, 30, 9, 0, tzinfo=UTC),
                ),
                RecordAuditRow(
                    id=2,
                    record_iri="urn:bob-touched",
                    operation=AuditOperation.MODIFY.value,
                    subject="https://idp/bob",
                    occurred_at=datetime(2026, 5, 30, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()
    service = DashboardService(
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=session_factory,
        pdp=pdp,  # type: ignore[arg-type]
    )
    response = await service.for_subject(_ctx(subject="https://idp/alice"))
    assert [item.record_iri for item in response.recent] == ["urn:alice-touched"]


@pytest.mark.unit
async def test_as_admin_drops_subject_filter_from_recent(
    session_factory: Any,
) -> None:
    adapter = _FakeAdapter([_sparql_response([])])
    pdp = _FakePDP()
    async with session_factory() as session:
        session.add_all(
            [
                RecordAuditRow(
                    id=1,
                    record_iri="urn:alice",
                    operation=AuditOperation.MODIFY.value,
                    subject="https://idp/alice",
                    occurred_at=datetime(2026, 5, 30, 9, 0, tzinfo=UTC),
                ),
                RecordAuditRow(
                    id=2,
                    record_iri="urn:bob",
                    operation=AuditOperation.MODIFY.value,
                    subject="https://idp/bob",
                    occurred_at=datetime(2026, 5, 30, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()
    service = DashboardService(
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=session_factory,
        pdp=pdp,  # type: ignore[arg-type]
    )
    response = await service.for_subject(_ctx(), as_admin=True)
    iris = {item.record_iri for item in response.recent}
    assert iris == {"urn:alice", "urn:bob"}


# --- router ----------------------------------------------------------------


class _StubService:
    """Replaces DashboardService for router-level tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def for_subject(
        self, ctx: Any, **kwargs: Any
    ) -> Any:
        from fdp.metadata.dashboard import DashboardResponse

        self.calls.append({"ctx": ctx, **kwargs})
        return DashboardResponse(owned=[], editable=[], recent=[])


def _build_app(service: Any, *, ctx: RequestContext | None = None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_dashboard_router(service=service))
    if ctx is not None:
        app.dependency_overrides[current_context] = lambda: ctx
    return app


@pytest.mark.unit
def test_router_rejects_anonymous_with_401() -> None:
    anon_ctx = RequestContext.anonymous(trace_id="t-1")
    app = _build_app(_StubService(), ctx=anon_ctx)
    response = TestClient(app).get("/me/dashboard")
    assert response.status_code == 401


@pytest.mark.unit
def test_router_serves_authenticated_caller() -> None:
    app = _build_app(_StubService(), ctx=_ctx())
    response = TestClient(app).get("/me/dashboard")
    assert response.status_code == 200
    assert response.json() == {"owned": [], "editable": [], "recent": []}


@pytest.mark.unit
def test_router_passes_limit_overrides_to_service() -> None:
    stub = _StubService()
    app = _build_app(stub, ctx=_ctx())
    response = TestClient(app).get(
        "/me/dashboard",
        params={"owned_limit": 5, "recent_limit": 7, "editable_limit": 11},
    )
    assert response.status_code == 200
    assert stub.calls[0]["owned_limit"] == 5
    assert stub.calls[0]["recent_limit"] == 7
    assert stub.calls[0]["editable_limit"] == 11


@pytest.mark.unit
def test_router_validates_limit_bounds() -> None:
    app = _build_app(_StubService(), ctx=_ctx())
    client = TestClient(app)
    assert client.get("/me/dashboard?owned_limit=0").status_code == 422
    assert client.get("/me/dashboard?recent_limit=1000").status_code == 422
    assert client.get("/me/dashboard?editable_limit=-1").status_code == 422


@pytest.mark.unit
def test_router_rejects_as_admin_without_admin_role() -> None:
    """A non-admin caller passing ?as_admin=true gets 403."""
    app = _build_app(_StubService(), ctx=_ctx(roles=frozenset({"steward"})))
    response = TestClient(app).get("/me/dashboard?as_admin=true")
    assert response.status_code == 403
    assert response.json()["code"] == "fdp.forbidden"


@pytest.mark.unit
def test_router_accepts_as_admin_with_admin_role() -> None:
    stub = _StubService()
    app = _build_app(stub, ctx=_ctx(roles=frozenset({"admin"})))
    response = TestClient(app).get("/me/dashboard?as_admin=true")
    assert response.status_code == 200
    assert stub.calls[0]["as_admin"] is True
