"""Unit tests for :class:`SecurityHeadersMiddleware` (audit F-05)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdp.shared.security_headers import SecurityHeadersMiddleware


def _app() -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/fdp-api/docs")  # stand-in for the Swagger UI path
    async def docs() -> dict[str, str]:
        return {"ui": "swagger"}

    return TestClient(app)


@pytest.mark.unit
def test_baseline_headers_present_on_api_response() -> None:
    h = _app().get("/thing").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "max-age=" in h["Strict-Transport-Security"]
    assert h["Content-Security-Policy"].startswith("default-src 'none'")


@pytest.mark.unit
def test_docs_ui_is_exempt_from_strict_csp() -> None:
    h = _app().get("/fdp-api/docs").headers
    # The doc UIs need CDN assets, so they keep the other headers but not the
    # locked-down CSP that would break them.
    assert "Content-Security-Policy" not in h
    assert h["X-Content-Type-Options"] == "nosniff"


@pytest.mark.unit
def test_headers_present_on_error_responses() -> None:
    h = _app().get("/missing").headers  # 404 from the inner app
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in h
