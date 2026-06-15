"""Unit tests for the bootstrap config endpoint (task 6.4).

Covers:

* Response shape and field population from ``Settings``.
* Profile block reflects current ``profile_applied`` row (or ``null``).
* ``client_id_hint`` is configurable.
* Endpoint is unauthenticated (anonymous requests succeed).
* No secrets surface in the payload.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from fdp.config import MetricsSettings, OIDCSettings, Settings
from fdp.identity.bootstrap import build_bootstrap_router
from fdp.metadata.profiles.state import ProfileAppliedRow

# --- fakes -----------------------------------------------------------------


class _FakeSession:
    """Minimal AsyncSession stand-in.

    The real ProfileStateRepository only calls ``execute(stmt)``; we
    intercept that and return a result whose ``scalar_one_or_none`` is
    whatever the test configured.
    """

    def __init__(self, row: ProfileAppliedRow | None) -> None:
        self._row = row

    async def execute(self, stmt: Any) -> Any:
        del stmt
        row = self._row

        class _Result:
            def scalar_one_or_none(self) -> ProfileAppliedRow | None:
                return row

        return _Result()


def _fake_session_factory(row: ProfileAppliedRow | None) -> Any:
    """Return an async_sessionmaker-shaped callable that yields ``_FakeSession``."""

    @asynccontextmanager
    async def _session() -> AsyncGenerator[_FakeSession, None]:
        yield _FakeSession(row)

    def _factory() -> Any:
        return _session()

    return _factory


def _settings(*, metrics_enabled: bool = True, identifier_base: str | None = None) -> Settings:
    """Construct a Settings instance with the minimum required fields.

    The test env (tests/conftest.py) already provides FDP_OIDC_*,
    POSTGRES_DSN, FDP_TRIPLESTORE_*; this just reads them.
    """
    return Settings(
        postgres_dsn="postgresql+asyncpg://fdp:fdp@localhost/fdp",  # type: ignore[arg-type]
        base_url=HttpUrl("http://localhost:8000"),
        identifier_base=HttpUrl(identifier_base) if identifier_base else None,
        oidc=OIDCSettings(  # type: ignore[call-arg]
            issuer=HttpUrl("http://idp.local/realms/fdp/"),
            audience="fdp",
        ),
        metrics=MetricsSettings(enabled=metrics_enabled),
    )


def _build_app(*, row: ProfileAppliedRow | None, identifier_base: str | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_bootstrap_router(
            settings=_settings(identifier_base=identifier_base),
            session_factory=_fake_session_factory(row),
        )
    )
    return app


# --- tests -----------------------------------------------------------------


@pytest.mark.unit
def test_returns_minimum_payload_when_no_profile_applied() -> None:
    app = _build_app(row=None)
    response = TestClient(app).get("/config")
    assert response.status_code == 200
    body = response.json()
    assert body["profile"] is None
    assert body["fdp_url"]  # populated, non-empty
    assert body["fdp_namespace"]
    assert body["fdp_version"]


@pytest.mark.unit
def test_fdp_url_equals_serving_url_in_dev() -> None:
    # No identifier_base configured → the persistent base falls back to the
    # serving origin, so the two coincide (localhost-friendly default).
    body = TestClient(_build_app(row=None)).get("/config").json()
    assert body["fdp_url"] == body["serving_url"] == "http://localhost:8000"


@pytest.mark.unit
def test_pid_base_decoupled_from_serving_url() -> None:
    # With a PID namespace configured, fdp_url is the persistent identifier base
    # while serving_url stays the deployment origin (ADR-0014).
    body = (
        TestClient(_build_app(row=None, identifier_base="https://w3id.org/myfdp"))
        .get("/config")
        .json()
    )
    assert body["fdp_url"] == "https://w3id.org/myfdp"
    assert body["serving_url"] == "http://localhost:8000"


@pytest.mark.unit
def test_oidc_block_reflects_settings() -> None:
    app = _build_app(row=None)
    body = TestClient(app).get("/config").json()
    oidc = body["oidc"]
    # Trailing slash is stripped so client-side URL composition is unambiguous.
    assert oidc["issuer"] == "http://idp.local/realms/fdp"
    assert oidc["audience"] == "fdp"
    assert oidc["client_id_hint"] == "fdp-client"


@pytest.mark.unit
def test_profile_block_populated_when_applied() -> None:
    row = ProfileAppliedRow(
        id=1,
        name="default",
        version="0.1.0",
        applied_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
        manifest_checksum="a" * 64,
    )
    app = _build_app(row=row)
    body = TestClient(app).get("/config").json()
    assert body["profile"] == {"name": "default", "version": "0.1.0"}


@pytest.mark.unit
def test_features_reflect_settings() -> None:
    app = _build_app(row=None)
    body = TestClient(app).get("/config").json()
    features = body["features"]
    # Metrics tracks the setting; sparql + data_provider are always on
    # in this v1 because the routers are unconditionally mounted.
    assert features["metrics"] is True
    assert features["sparql"] is True
    assert features["data_provider"] is True
    # Search ships (Phase 7) and tracks settings.search.enabled (default on).
    assert features["search"] is True
    # Index (Phase 8 — FDP Index protocol) is not implemented.
    assert features["index"] is False


@pytest.mark.unit
def test_metrics_flag_follows_disabled_setting() -> None:
    app = FastAPI()
    app.include_router(
        build_bootstrap_router(
            settings=_settings(metrics_enabled=False),
            session_factory=_fake_session_factory(None),
        )
    )
    body = TestClient(app).get("/config").json()
    assert body["features"]["metrics"] is False


@pytest.mark.unit
def test_client_id_hint_is_configurable() -> None:
    app = FastAPI()
    app.include_router(
        build_bootstrap_router(
            settings=_settings(),
            session_factory=_fake_session_factory(None),
            client_id_hint="custom-client-id",
        )
    )
    body = TestClient(app).get("/config").json()
    assert body["oidc"]["client_id_hint"] == "custom-client-id"


@pytest.mark.unit
def test_client_id_hint_can_be_null() -> None:
    app = FastAPI()
    app.include_router(
        build_bootstrap_router(
            settings=_settings(),
            session_factory=_fake_session_factory(None),
            client_id_hint=None,
        )
    )
    body = TestClient(app).get("/config").json()
    assert body["oidc"]["client_id_hint"] is None


@pytest.mark.unit
def test_endpoint_is_anonymous_friendly() -> None:
    """No Authorization header → 200, not 401.

    The bootstrap endpoint must be reachable pre-login so the client
    can discover the IdP. The test app does not install the auth
    middleware; this assertion documents the contract.
    """
    app = _build_app(row=None)
    response = TestClient(app).get("/config")
    assert response.status_code == 200


@pytest.mark.unit
def test_no_secrets_in_payload() -> None:
    """Defensive: assert that no obviously-sensitive keys appear.

    If a future change adds a field like ``client_secret`` or
    ``signing_key`` to the response, this test will fail loudly. It is
    a structural guard, not a comprehensive security review.
    """
    row = ProfileAppliedRow(
        id=1,
        name="default",
        version="0.1.0",
        applied_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
        manifest_checksum="a" * 64,
    )
    app = _build_app(row=row)
    body = TestClient(app).get("/config").json()
    flat_keys = _all_keys(body)
    forbidden = {"secret", "password", "private_key", "signing_key", "client_secret"}
    leaked = {k for k in flat_keys if any(f in k.lower() for f in forbidden)}
    assert not leaked, f"sensitive-looking keys in payload: {leaked}"


def _all_keys(obj: Any, prefix: str = "") -> set[str]:
    """Recursively collect all keys in a nested dict."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.add(path)
            keys |= _all_keys(v, path)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item, prefix)
    return keys
