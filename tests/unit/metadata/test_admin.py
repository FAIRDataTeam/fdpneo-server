"""Unit tests for the factory-reset admin surface (Phase 10.4).

Covers:

* ``SettingsRepository.clear_all`` against in-memory SQLite (truncates every
  override row, returns the count).
* The ``POST /admin/reset`` router over a fake :class:`ResetService`: anonymous
  → 401, non-admin → 403, wrong/missing confirmation token → 400/422, and the
  happy path (admin + correct token) → 200 with the service's report and the
  acting subject threaded through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.identity.deps import current_context
from fdp.metadata.admin import (
    RESET_CONFIRMATION_TOKEN,
    ResetResponse,
    ResetService,
    build_admin_router,
)
from fdp.metadata.settings import (
    AutocompleteSources,
    SearchFilters,
    SettingsRepository,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import register_exception_handlers
from fdp.storage.postgres.models import Base

# --- fixtures --------------------------------------------------------------


@pytest.fixture
async def session_factory() -> Any:
    """In-memory SQLite with the runtime_settings table created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _ctx(
    *, subject: str | None = "https://idp/alice", roles: frozenset[str] = frozenset()
) -> RequestContext:
    return RequestContext(
        subject=subject,
        roles=roles,
        trace_id="t-1",
        request_timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )


class _FakeResetService:
    """Records the subject it was called with and returns a canned report."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def reset(self, *, subject: str | None) -> ResetResponse:
        self.calls.append(subject)
        return ResetResponse(
            profile_name="default",
            profile_version="0.1.0",
            settings_cleared=2,
            schemas=5,
            offers=1,
            resource_definitions=3,
            seed_records=0,
        )


def _build_app(service: Any, *, ctx: RequestContext) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_admin_router(service=service))
    app.dependency_overrides[current_context] = lambda: ctx
    return TestClient(app)


# --- clear_all -------------------------------------------------------------


@pytest.mark.unit
async def test_clear_all_truncates_overrides(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    await repo.write("forms.autocomplete-sources", AutocompleteSources(), subject="admin")
    await repo.write("search.filters", SearchFilters(), subject="admin")

    removed = await repo.clear_all(subject="https://idp/admin")

    assert removed == 2
    # Both keys fall back to their registered defaults afterwards.
    assert await repo.read("forms.autocomplete-sources") is None
    assert await repo.read("search.filters") is None


@pytest.mark.unit
async def test_clear_all_on_empty_table_returns_zero(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    assert await repo.clear_all() == 0


@pytest.mark.unit
def test_session_factory_property_is_exposed(session_factory: Any) -> None:
    repo = SettingsRepository(session_factory=session_factory)
    assert repo.session_factory is session_factory


# --- router auth + confirmation -------------------------------------------


@pytest.mark.unit
def test_reset_requires_authentication() -> None:
    service = _FakeResetService()
    client = _build_app(service, ctx=_ctx(subject=None))
    resp = client.post("/admin/reset", json={"confirmation": RESET_CONFIRMATION_TOKEN})
    assert resp.status_code == 401
    assert service.calls == []


@pytest.mark.unit
def test_reset_requires_admin_role() -> None:
    service = _FakeResetService()
    client = _build_app(service, ctx=_ctx(roles=frozenset({"steward"})))
    resp = client.post("/admin/reset", json={"confirmation": RESET_CONFIRMATION_TOKEN})
    assert resp.status_code == 403
    assert service.calls == []


@pytest.mark.unit
def test_reset_rejects_wrong_confirmation_token() -> None:
    service = _FakeResetService()
    client = _build_app(service, ctx=_ctx(roles=frozenset({"admin"})))
    resp = client.post("/admin/reset", json={"confirmation": "nope"})
    assert resp.status_code == 400
    # Service is never touched when the token is wrong.
    assert service.calls == []


@pytest.mark.unit
def test_reset_rejects_missing_confirmation() -> None:
    service = _FakeResetService()
    client = _build_app(service, ctx=_ctx(roles=frozenset({"admin"})))
    resp = client.post("/admin/reset", json={})
    assert resp.status_code == 422
    assert service.calls == []


@pytest.mark.unit
def test_reset_happy_path_returns_report_and_threads_subject() -> None:
    service = _FakeResetService()
    client = _build_app(service, ctx=_ctx(subject="https://idp/admin", roles=frozenset({"admin"})))
    resp = client.post("/admin/reset", json={"confirmation": RESET_CONFIRMATION_TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["profileName"] == "default"
    assert body["settingsCleared"] == 2
    assert body["schemas"] == 5
    assert body["resourceDefinitions"] == 3
    assert service.calls == ["https://idp/admin"]


# --- service: no-bundle guard ---------------------------------------------


@pytest.mark.unit
async def test_service_refuses_without_configured_bundle(session_factory: Any) -> None:
    from types import SimpleNamespace

    from fdp.shared.errors import Conflict

    # A settings stand-in with no profile bundle configured.
    settings = SimpleNamespace(profile=SimpleNamespace(path=None))

    published: list[Any] = []

    async def _on_published(sdoi: Any, rd: Any) -> None:
        published.append((sdoi, rd))

    service = ResetService(
        settings=settings,  # type: ignore[arg-type]
        settings_repository=SettingsRepository(session_factory=session_factory),
        repository=object(),  # type: ignore[arg-type]
        on_published=_on_published,
    )
    with pytest.raises(Conflict):
        await service.reset(subject="https://idp/admin")
    assert published == []
