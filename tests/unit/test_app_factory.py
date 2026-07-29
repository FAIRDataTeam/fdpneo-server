"""Smoke tests for the FastAPI application factory.

These run before anything else is built — they fail loudly if the app cannot
even start, which catches a lot of dependency-injection mistakes early.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_app_factory_returns_app() -> None:
    """``create_app`` returns a FastAPI instance with the expected metadata."""
    from fdpneo_server import __version__
    from fdpneo_server.main import create_app

    app = create_app()
    assert app.title == "FAIR Data Point"
    assert app.version == __version__


@pytest.mark.unit
def test_healthz_returns_ok() -> None:
    """The liveness probe answers without touching downstream dependencies."""
    from fdpneo_server.main import create_app

    client = TestClient(create_app())
    response = client.get("/fdp-api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.unit
def test_cors_preflight_allows_configured_origin() -> None:
    """A browser preflight (OPTIONS) for a write is answered with CORS headers.

    This is the regression guard for the SPA "server unreachable" bug: the
    cross-origin preflight must succeed *before* auth so the browser permits
    the follow-up PUT.
    """
    from fdpneo_server.main import create_app

    client = TestClient(create_app())
    response = client.options(
        "/meta",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type,if-match",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.unit
def test_cors_headers_on_simple_request() -> None:
    """A simple cross-origin GET carries the allow-origin header on the response."""
    from fdpneo_server.main import create_app

    client = TestClient(create_app())
    response = client.get("/fdp-api/healthz", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.unit
def test_cors_allows_loopback_ip_origin() -> None:
    """``127.0.0.1`` is allowed alongside ``localhost``.

    Browsers treat the two loopback spellings as distinct origins; the SPA may
    be opened under either, so both must be permitted or writes fail with a
    CORS rejection that surfaces as "server unreachable".
    """
    from fdpneo_server.main import create_app

    client = TestClient(create_app())
    response = client.options(
        "/meta",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type,if-match",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


@pytest.mark.unit
def test_cors_rejects_unconfigured_origin() -> None:
    """An origin that isn't allow-listed gets no allow-origin header echoed back."""
    from fdpneo_server.main import create_app

    client = TestClient(create_app())
    response = client.get("/fdp-api/healthz", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
