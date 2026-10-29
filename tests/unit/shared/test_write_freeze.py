"""Unit tests for the write freeze used by online full backups (ADR-0029 §6)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fdpneo_server.shared.write_freeze import (
    RETRY_AFTER_SECONDS,
    WriteFreeze,
    WriteFreezeMiddleware,
    is_write_request,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/catalog/x", False),
        ("HEAD", "/", False),
        ("OPTIONS", "/catalog", False),
        ("POST", "/catalog", True),
        ("PUT", "/catalog/x", True),
        ("PATCH", "/catalog/x", True),
        ("DELETE", "/catalog/x", True),
        ("POST", "/fdp-api/sparql", False),
        ("POST", "/fdp-api/search", False),
        ("POST", "/fdp-api/data/d1/sparql", False),
        ("POST", "/fdp-api/settings/x", True),
        ("POST", "/fdp-api/admin/backup/dump", True),
    ],
)
def test_is_write_request(method: str, path: str, *, expected: bool) -> None:
    assert is_write_request(method, path) is expected


def _app(freeze: WriteFreeze) -> FastAPI:
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anything(path: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"path": path}

    app.add_middleware(WriteFreezeMiddleware, freeze=freeze)
    return app


async def test_writes_get_503_while_frozen_reads_pass() -> None:
    freeze = WriteFreeze()
    client = TestClient(_app(freeze))
    assert client.post("/catalog").status_code == 200
    async with freeze.hold("a full backup"):
        resp = client.put("/catalog/x")
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
        body = resp.json()
        assert body["code"] == "fdp.service_unavailable"
        assert "a full backup" in body["message"]
        assert client.get("/catalog/x").status_code == 200
        assert client.post("/fdp-api/sparql").status_code == 200
    assert client.put("/catalog/x").status_code == 200


async def test_freeze_holders_nest() -> None:
    freeze = WriteFreeze()
    async with freeze.hold("one"):
        async with freeze.hold("two"):
            assert freeze.active
        assert freeze.active
    assert not freeze.active
    assert freeze.reason is None
