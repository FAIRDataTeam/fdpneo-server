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
    from fdp import __version__
    from fdp.main import create_app

    app = create_app()
    assert app.title == "FAIR Data Point"
    assert app.version == __version__


@pytest.mark.unit
def test_healthz_returns_ok() -> None:
    """The liveness probe answers without touching downstream dependencies."""
    from fdp.main import create_app

    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
