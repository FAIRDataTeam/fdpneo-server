"""Unit tests for the request-limit middlewares (audit R-02)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from fdp.shared.limits import BodySizeLimitMiddleware, RateLimitMiddleware

# --- rate limiting ---------------------------------------------------------


def _rate_app(limit: int) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_window=limit, window_seconds=60)

    @app.get("/ping")
    async def ping() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": "yes"}

    return TestClient(app)


@pytest.mark.unit
def test_under_limit_passes_then_429_over_limit() -> None:
    client = _rate_app(limit=3)
    assert [client.get("/ping").status_code for _ in range(3)] == [200, 200, 200]
    resp = client.get("/ping")  # 4th in the same window
    assert resp.status_code == 429
    assert resp.json()["code"] == "fdp.too_many_requests"
    assert "retry-after" in {k.lower() for k in resp.headers}


# --- body size cap ---------------------------------------------------------


def _body_app(max_bytes: int) -> TestClient:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        body = await request.body()
        return {"len": len(body)}

    return TestClient(app)


@pytest.mark.unit
def test_body_under_cap_ok() -> None:
    r = _body_app(max_bytes=100).post("/echo", content=b"x" * 50)
    assert r.status_code == 200 and r.json()["len"] == 50


@pytest.mark.unit
def test_body_over_cap_rejected_via_content_length() -> None:
    r = _body_app(max_bytes=100).post("/echo", content=b"x" * 200)  # TestClient sets CL
    assert r.status_code == 413
    assert r.json()["code"] == "fdp.payload_too_large"


@pytest.mark.unit
async def test_streaming_body_over_cap_rejected_without_content_length() -> None:
    # Drive the middleware as raw ASGI with chunked body and no Content-Length,
    # so the streaming byte-counter (not the fast path) does the rejecting.
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodySizeLimitMiddleware(app, max_bytes=10)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"y" * 6, "more_body": False},
        ]
    )

    async def receive() -> Message:
        return next(chunks)

    async def send(message: Message) -> None:
        sent.append(message)

    await mw(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
