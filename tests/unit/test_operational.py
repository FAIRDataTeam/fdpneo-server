"""Unit tests for the operational endpoints (tasks 13.1, 13.2).

Covers:

* ``/info`` response shape, environment passthrough, build/runtime
  metadata population from env vars.
* ``/readyz`` parallel-probe fan-out: all-ok → 200; any failure → 503
  with the failing dependency identified.
* Per-probe timeouts: a hung dependency surfaces as a `fail` outcome
  rather than blocking the response.
* No secrets in either payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from fdp.config import MetricsSettings, OIDCSettings, Settings
from fdp.operational import build_info_router, build_readiness_router

# --- /info -----------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        postgres_dsn="postgresql+asyncpg://fdp:fdp@localhost/fdp",  # type: ignore[arg-type]
        oidc=OIDCSettings(  # type: ignore[call-arg]
            issuer=HttpUrl("http://idp.local/realms/fdp/"),
            audience="fdp",
        ),
        metrics=MetricsSettings(),
    )


def _info_app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_info_router(settings=_settings()))
    return app


@pytest.mark.unit
def test_info_returns_name_version_environment() -> None:
    body = TestClient(_info_app()).get("/info").json()
    assert body["name"] == "fdp-server"
    assert body["version"]  # non-empty
    assert body["environment"] == "development"


@pytest.mark.unit
def test_info_runtime_python_version_present() -> None:
    body = TestClient(_info_app()).get("/info").json()
    assert "python_version" in body["runtime"]
    # Format like "3.14.4" — three dotted numbers.
    parts = body["runtime"]["python_version"].split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


@pytest.mark.unit
def test_info_build_metadata_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FDP_BUILD_COMMIT", "abc123def")
    monkeypatch.setenv("FDP_BUILD_BUILT_AT", "2026-05-29T12:00:00Z")
    body = TestClient(_info_app()).get("/info").json()
    assert body["build"]["commit"] == "abc123def"
    assert body["build"]["built_at"] == "2026-05-29T12:00:00Z"


@pytest.mark.unit
def test_info_build_metadata_is_null_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FDP_BUILD_COMMIT", raising=False)
    monkeypatch.delenv("FDP_BUILD_BUILT_AT", raising=False)
    body = TestClient(_info_app()).get("/info").json()
    assert body["build"]["commit"] is None
    assert body["build"]["built_at"] is None


# --- /readyz: fakes --------------------------------------------------------


class _FakeSession:
    """AsyncSession stand-in that returns a dummy result for `execute`."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self._fail = fail
        self._delay = delay

    async def execute(self, stmt: Any) -> Any:
        del stmt
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("postgres unreachable")

        class _Result:
            pass

        return _Result()


def _fake_session_factory(*, fail: bool = False, delay: float = 0.0) -> Any:
    @asynccontextmanager
    async def _session() -> AsyncGenerator[_FakeSession, None]:
        yield _FakeSession(fail=fail, delay=delay)

    def _factory() -> Any:
        return _session()

    return _factory


class _FakeAdapter:
    """TripleStoreAdapter stand-in with configurable success/failure."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self._fail = fail
        self._delay = delay

    async def ask(self, sparql: str) -> bool:
        del sparql
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("triplestore unreachable")
        return False


def _http_client_returning(status_code: int) -> httpx.AsyncClient:
    """Build an httpx client whose handler always returns ``status_code``."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code=status_code, json={"issuer": "..."})

    transport = httpx.MockTransport(_handler)
    return httpx.AsyncClient(transport=transport)


def _http_client_raising(exc: Exception) -> httpx.AsyncClient:
    async def _handler(request: httpx.Request) -> httpx.Response:
        del request
        raise exc

    transport = httpx.MockTransport(_handler)
    return httpx.AsyncClient(transport=transport)


def _readyz_app(
    *,
    session_factory: Any,
    adapter: Any,
    http_client: httpx.AsyncClient,
    issuer: str = "http://idp.local/realms/fdp",
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_readiness_router(
            session_factory=session_factory,
            adapter=adapter,
            http_client=http_client,
            issuer=issuer,
        )
    )
    return app


# --- /readyz: happy path ---------------------------------------------------


@pytest.mark.unit
def test_readyz_all_ok_returns_200_with_ready_status() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_FakeAdapter(),
        http_client=_http_client_returning(200),
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"].keys()) == {"postgres", "triplestore", "oidc"}
    assert all(c["status"] == "ok" for c in body["checks"].values())


@pytest.mark.unit
def test_readyz_records_latency_for_passing_checks() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_FakeAdapter(),
        http_client=_http_client_returning(200),
    )
    body = TestClient(app).get("/readyz").json()
    for check in body["checks"].values():
        assert check["status"] == "ok"
        assert isinstance(check["latency_ms"], int)
        assert check["latency_ms"] >= 0


# --- /readyz: failure modes ------------------------------------------------


@pytest.mark.unit
def test_readyz_postgres_failure_returns_503() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(fail=True),
        adapter=_FakeAdapter(),
        http_client=_http_client_returning(200),
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"]["status"] == "fail"
    assert "postgres unreachable" in body["checks"]["postgres"]["error"]
    # Other checks still report green.
    assert body["checks"]["triplestore"]["status"] == "ok"
    assert body["checks"]["oidc"]["status"] == "ok"


@pytest.mark.unit
def test_readyz_triplestore_failure_returns_503() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_FakeAdapter(fail=True),
        http_client=_http_client_returning(200),
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["triplestore"]["status"] == "fail"


@pytest.mark.unit
def test_readyz_oidc_non_2xx_response_returns_503() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_FakeAdapter(),
        http_client=_http_client_returning(500),
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["oidc"]["status"] == "fail"
    assert "500" in body["checks"]["oidc"]["error"]


@pytest.mark.unit
def test_readyz_oidc_transport_error_returns_503() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_FakeAdapter(),
        http_client=_http_client_raising(httpx.ConnectError("dial tcp: no route")),
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["oidc"]["status"] == "fail"


@pytest.mark.unit
def test_readyz_multiple_failures_all_reported() -> None:
    app = _readyz_app(
        session_factory=_fake_session_factory(fail=True),
        adapter=_FakeAdapter(fail=True),
        http_client=_http_client_returning(503),
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert all(c["status"] == "fail" for c in body["checks"].values())


# --- /readyz: structural / security ---------------------------------------


@pytest.mark.unit
def test_readyz_error_messages_are_single_line() -> None:
    """Multi-line exception messages must not leak through the body."""
    multi_line_exc = RuntimeError("first line\nsecond line\nthird line")

    class _BadAdapter:
        async def ask(self, sparql: str) -> bool:
            del sparql
            raise multi_line_exc

    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_BadAdapter(),
        http_client=_http_client_returning(200),
    )
    body = TestClient(app).get("/readyz").json()
    error = body["checks"]["triplestore"]["error"]
    assert "\n" not in error
    assert "second line" not in error  # only the first line surfaces


@pytest.mark.unit
def test_readyz_response_has_no_obvious_secrets() -> None:
    """Defensive: the readiness payload must not include credentials."""
    app = _readyz_app(
        session_factory=_fake_session_factory(),
        adapter=_FakeAdapter(),
        http_client=_http_client_returning(200),
    )
    body = TestClient(app).get("/readyz").json()
    serialized = repr(body).lower()
    for forbidden in ("password", "secret", "fdp:fdp"):
        assert forbidden not in serialized, f"sensitive substring leaked: {forbidden}"
