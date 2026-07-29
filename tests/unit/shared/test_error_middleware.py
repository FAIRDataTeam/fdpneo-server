"""Unit tests for the catch-all error envelope middleware (audit R-08)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from fdpneo_server.shared.errors import CatchAllExceptionMiddleware, Conflict

ORIGIN = "http://app.example"


def _client(*, with_cors: bool = False) -> TestClient:
    app = FastAPI()

    @app.get("/boom")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("secret internal detail")

    @app.get("/conflict")
    async def conflict() -> None:  # pyright: ignore[reportUnusedFunction]
        raise Conflict("dup", details={"x": 1})

    # Deliberately NO register_exception_handlers: exercise the middleware path
    # for both an unexpected error and an FDPError that escapes the routes.
    app.add_middleware(CatchAllExceptionMiddleware)
    if with_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[ORIGIN],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
def test_unexpected_exception_becomes_500_envelope_without_leak() -> None:
    r = _client().get("/boom")
    assert r.status_code == 500
    assert r.json()["code"] == "fdp.internal_error"
    assert "secret internal detail" not in r.text  # internals never returned


@pytest.mark.unit
def test_fdp_error_keeps_its_status_and_envelope() -> None:
    r = _client().get("/conflict")
    assert r.status_code == 409
    assert r.json()["code"] == "fdp.conflict"
    assert r.json()["details"] == {"x": 1}


@pytest.mark.unit
def test_cors_headers_present_on_500() -> None:
    r = _client(with_cors=True).get("/boom", headers={"Origin": ORIGIN})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == ORIGIN
