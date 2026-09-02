"""Unit tests for runtime-managed FDP Index targets (ADR-0025)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdpneo_server.config import IndexSettings
from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.index_ping import PingResult
from fdpneo_server.metadata.index_targets import (
    IndexTargetRepository,
    IndexTargetService,
    build_index_targets_router,
    normalize_target,
)
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import (
    BadRequest,
    Conflict,
    NotFound,
    UpstreamError,
    register_exception_handlers,
)
from fdpneo_server.storage.postgres.models import Base, register_all_models

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
async def session_factory() -> Any:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _no_guard(url: str, **kwargs: object) -> None:
    del url, kwargs


def _service(
    session_factory: Any,
    *,
    env_targets: str = "",
    url_guard: Any = _no_guard,
) -> IndexTargetService:
    return IndexTargetService(
        repository=IndexTargetRepository(session_factory=session_factory),
        settings=IndexSettings(_env_file=None, ping_targets=env_targets),  # type: ignore[arg-type]
        clock=lambda: NOW,
        url_guard=url_guard,
    )


# --- service ------------------------------------------------------------------


async def test_add_normalizes_and_persists(session_factory: Any) -> None:
    service = _service(session_factory)
    info = await service.add(url="HTTPS://Index.Example/base/", note="n", subject="alice")
    assert info.url == "https://index.example/base"
    assert info.source == "runtime"
    assert info.created_by == "alice"
    assert info.created_at == NOW
    assert [t.url for t in await service.list_targets()] == ["https://index.example/base"]


async def test_add_duplicate_runtime_conflicts(session_factory: Any) -> None:
    service = _service(session_factory)
    await service.add(url="https://idx.example", note=None, subject=None)
    with pytest.raises(Conflict):
        await service.add(url="https://idx.example/", note=None, subject=None)


async def test_add_duplicating_env_target_conflicts(session_factory: Any) -> None:
    service = _service(session_factory, env_targets="https://idx.example/")
    with pytest.raises(Conflict):
        await service.add(url="https://idx.example", note=None, subject=None)


async def test_add_rejects_non_http_scheme(session_factory: Any) -> None:
    service = _service(session_factory)
    with pytest.raises(BadRequest):
        await service.add(url="ftp://idx.example", note=None, subject=None)


async def test_add_maps_ssrf_upstream_error_to_bad_request(session_factory: Any) -> None:
    """assert_public_url raises UpstreamError (502 semantics — right for
    server-supplied metadata); at this admin boundary the URL is the caller's
    own body, so a private-IP target must surface as 400, not 502."""

    async def guard(url: str, **kwargs: object) -> None:
        del url, kwargs
        raise UpstreamError("resolves to a non-public address")

    service = _service(session_factory, url_guard=guard)
    with pytest.raises(BadRequest):
        await service.add(url="https://internal.example", note=None, subject=None)


async def test_remove_unknown_not_found(session_factory: Any) -> None:
    service = _service(session_factory)
    with pytest.raises(NotFound):
        await service.remove("nope")


async def test_effective_urls_unions_env_and_runtime_deduped(session_factory: Any) -> None:
    service = _service(session_factory, env_targets="https://env.example, https://both.example")
    await service.add(url="https://runtime.example", note=None, subject=None)
    # An env duplicate can't be *added*, but dedupe still guards the union.
    assert list(await service.effective_urls()) == [
        "https://env.example",
        "https://both.example",
        "https://runtime.example",
    ]


async def test_record_results_updates_runtime_row_and_env_memory(session_factory: Any) -> None:
    service = _service(session_factory, env_targets="https://env.example")
    added = await service.add(url="https://runtime.example", note=None, subject=None)
    await service.record_results(
        [
            PingResult(target="https://env.example", status=204, ok=True),
            PingResult(target="https://runtime.example", status=429, ok=False, detail="HTTP 429"),
        ]
    )
    by_url = {t.url: t for t in await service.list_targets()}
    env = by_url["https://env.example"]
    assert env.source == "env" and env.last_ok is True and env.last_status_code == 204
    run = by_url["https://runtime.example"]
    assert run.id == added.id
    assert run.last_ok is False and run.last_status_code == 429 and run.last_detail == "HTTP 429"
    assert run.last_ping_at == NOW


def test_normalize_target_examples() -> None:
    assert normalize_target(" HTTPS://Idx.Example/ ") == "https://idx.example"
    assert normalize_target("http://idx.example/a/b/") == "http://idx.example/a/b"


# --- router --------------------------------------------------------------------


class _FakePinger:
    def __init__(self, results: list[PingResult] | None = None) -> None:
        self.results = results or []
        self.calls: list[str] = []

    async def ping_now(self, reason: str = "manual") -> list[PingResult]:
        self.calls.append(reason)
        return self.results


def _app(service: IndexTargetService, pinger: Any, ctx: RequestContext) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_index_targets_router(service=service, pinger=pinger))
    app.dependency_overrides[require_auth] = lambda: ctx
    return app


def _ctx(*, roles: frozenset[str]) -> RequestContext:
    return RequestContext(subject="subj", roles=roles, trace_id="t", request_timestamp=NOW)


async def test_router_requires_admin(session_factory: Any) -> None:
    app = _app(_service(session_factory), _FakePinger(), _ctx(roles=frozenset({"steward"})))
    with TestClient(app) as client:
        assert client.get("/index/targets").status_code == 403
        assert client.post("/index/targets", json={"url": "https://x.example"}).status_code == 403
        assert client.delete("/index/targets/x").status_code == 403
        assert client.post("/index/ping").status_code == 403


async def test_router_crud_roundtrip_as_admin(session_factory: Any) -> None:
    service = _service(session_factory, env_targets="https://env.example")
    app = _app(service, _FakePinger(), _ctx(roles=frozenset({"admin"})))
    with TestClient(app) as client:
        created = client.post(
            "/index/targets", json={"url": "https://idx.example/", "note": "home index"}
        )
        assert created.status_code == 201
        target_id = created.json()["id"]
        assert created.json()["url"] == "https://idx.example"

        listing = client.get("/index/targets").json()["targets"]
        assert [(t["source"], t["url"]) for t in listing] == [
            ("env", "https://env.example"),
            ("runtime", "https://idx.example"),
        ]

        assert client.post("/index/targets", json={"url": "https://idx.example"}).status_code == 409
        assert client.post("/index/targets", json={"url": "not a url"}).status_code == 400

        assert client.delete(f"/index/targets/{target_id}").status_code == 204
        assert client.delete(f"/index/targets/{target_id}").status_code == 404


async def test_router_ping_now_returns_per_target_results(session_factory: Any) -> None:
    pinger = _FakePinger(
        [
            PingResult(target="https://idx.example", status=204, ok=True),
            PingResult(target="https://down.example", status=None, ok=False, detail="unreachable"),
        ]
    )
    app = _app(_service(session_factory), pinger, _ctx(roles=frozenset({"admin"})))
    with TestClient(app) as client:
        response = client.post("/index/ping")
    assert response.status_code == 200
    assert pinger.calls == ["admin"]
    results = response.json()["results"]
    assert results[0] == {
        "target": "https://idx.example",
        "status": 204,
        "ok": True,
        "detail": None,
    }
    assert results[1]["ok"] is False and results[1]["detail"] == "unreachable"
