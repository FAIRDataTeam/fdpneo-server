"""Unit tests for :class:`RequestObservationMiddleware`.

Tests drive the middleware directly through its ASGI interface so the
fire-and-forget publish task runs on the same event loop the test
controls; ``asyncio.sleep(0)`` after each invocation gives that task
one tick to complete.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from fdp.metrics.events import MetricEventType, RequestObserved
from fdp.metrics.middleware import RequestObservationMiddleware
from fdp.shared.context import RequestContext, bound
from fdp.shared.events import EventBus


# --- ASGI driver helpers ---------------------------------------------------


class _Recorder:
    """Subscribes to ``RequestObserved`` and stores everything it sees."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[RequestObserved] = []
        self._sub = bus.subscribe(RequestObserved, self._handle)

    async def _handle(self, event: RequestObserved) -> None:
        self.events.append(event)


def _scope(
    *,
    method: str = "GET",
    path: str = "/anything-else",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 12345),
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [(b"host", b"testserver"), *(headers or [])],
        "client": client,
    }


def _echo_app(
    status: int = 200,
    chunks: list[bytes] | None = None,
) -> Callable[[dict[str, Any], Any, Any], Awaitable[None]]:
    """Return a minimal ASGI app that responds with ``status`` + chunks."""
    body_chunks = chunks if chunks is not None else [b""]

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        for chunk in body_chunks[:-1]:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send(
            {"type": "http.response.body", "body": body_chunks[-1], "more_body": False}
        )

    return app


async def _drive(
    middleware: Callable[[dict[str, Any], Any, Any], Awaitable[None]],
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    """Call ``middleware`` once and return every message it sent downstream."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    # The middleware's finally schedules `asyncio.create_task(_publish_safe(...))`.
    # One yield is enough to let that task run on the same loop.
    await asyncio.sleep(0)
    return sent


def _build(
    *,
    inner: Callable[..., Awaitable[None]] | None = None,
    bus: EventBus | None = None,
) -> tuple[RequestObservationMiddleware, _Recorder, EventBus]:
    bus = bus or EventBus()
    recorder = _Recorder(bus)
    mw = RequestObservationMiddleware(
        inner or _echo_app(),  # type: ignore[arg-type]
        bus_provider=lambda: bus,
    )
    return mw, recorder, bus


# --- skip list ------------------------------------------------------------


@pytest.mark.unit
async def test_healthz_is_skipped() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(path="/healthz"))
    assert recorder.events == []


@pytest.mark.unit
async def test_metrics_dashboard_is_skipped() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(path="/metrics/summary"))
    assert recorder.events == []


@pytest.mark.unit
async def test_openapi_paths_are_skipped() -> None:
    mw, recorder, _ = _build()
    for path in ("/openapi.json", "/docs", "/redoc"):
        await _drive(mw, _scope(path=path))
    assert recorder.events == []


@pytest.mark.unit
async def test_options_is_skipped() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="OPTIONS", path="/anything-else"))
    assert recorder.events == []


# --- event-type mapping ---------------------------------------------------


@pytest.mark.unit
async def test_get_emits_view() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="GET", path="/anything-else"))
    assert len(recorder.events) == 1
    assert recorder.events[0].event_type is MetricEventType.VIEW


@pytest.mark.unit
async def test_post_emits_modify() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="POST", path="/some-container"))
    assert recorder.events[0].event_type is MetricEventType.MODIFY


@pytest.mark.unit
async def test_put_emits_modify() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="PUT", path="/some-record"))
    assert recorder.events[0].event_type is MetricEventType.MODIFY


@pytest.mark.unit
async def test_patch_emits_modify() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="PATCH", path="/some-record"))
    assert recorder.events[0].event_type is MetricEventType.MODIFY


@pytest.mark.unit
async def test_delete_emits_delete() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="DELETE", path="/some-record"))
    assert recorder.events[0].event_type is MetricEventType.DELETE


@pytest.mark.unit
async def test_top_level_sparql_emits_sparql_query_with_no_resource() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(method="GET", path="/sparql"))
    assert recorder.events[0].event_type is MetricEventType.SPARQL_QUERY
    assert recorder.events[0].resource_iri is None


@pytest.mark.unit
async def test_data_download_emits_download() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(path="/data/dist-1"))
    assert recorder.events[0].event_type is MetricEventType.DOWNLOAD


@pytest.mark.unit
async def test_data_distribution_sparql_emits_sparql_query() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(path="/data/dist-1/sparql"))
    assert recorder.events[0].event_type is MetricEventType.SPARQL_QUERY
    # The per-distribution SPARQL endpoint targets a known resource;
    # the iri should be present (the distribution's URL).
    assert recorder.events[0].resource_iri is not None
    assert "/data/dist-1/sparql" in (recorder.events[0].resource_iri or "")


# --- captured fields ------------------------------------------------------


@pytest.mark.unit
async def test_status_code_is_captured() -> None:
    mw, recorder, _ = _build(inner=_echo_app(status=201))
    await _drive(mw, _scope(method="PUT", path="/r"))
    assert recorder.events[0].status_code == 201


@pytest.mark.unit
async def test_resource_iri_is_absolute_url_for_ldp_routes() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(path="/some/record"))
    iri = recorder.events[0].resource_iri or ""
    assert iri == "http://testserver/some/record"


@pytest.mark.unit
async def test_user_agent_header_is_captured() -> None:
    mw, recorder, _ = _build()
    await _drive(
        mw,
        _scope(headers=[(b"user-agent", b"TestUA/1.0")]),
    )
    assert recorder.events[0].user_agent == "TestUA/1.0"


@pytest.mark.unit
async def test_x_forwarded_for_takes_precedence_over_socket_ip() -> None:
    mw, recorder, _ = _build()
    await _drive(
        mw,
        _scope(
            headers=[(b"x-forwarded-for", b"203.0.113.42, 10.0.0.1")],
            client=("10.0.0.1", 12345),
        ),
    )
    assert recorder.events[0].ip == "203.0.113.42"


@pytest.mark.unit
async def test_falls_back_to_socket_ip_without_forwarded_header() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope(client=("198.51.100.7", 12345)))
    assert recorder.events[0].ip == "198.51.100.7"


# --- subject snapshot -----------------------------------------------------


@pytest.mark.unit
async def test_subject_is_picked_up_from_the_bound_context() -> None:
    mw, recorder, _ = _build()
    ctx = RequestContext(subject="https://idp/alice", roles=frozenset(), trace_id="t-1")
    with bound(ctx):
        await _drive(mw, _scope())
    assert recorder.events[0].subject == "https://idp/alice"


@pytest.mark.unit
async def test_anonymous_request_has_no_subject() -> None:
    mw, recorder, _ = _build()
    await _drive(mw, _scope())
    assert recorder.events[0].subject is None


# --- structural guarantees -----------------------------------------------


@pytest.mark.unit
async def test_response_messages_pass_through_unchanged() -> None:
    """The middleware must not buffer or rewrite the response — vital for
    streamed CONSTRUCT/DESCRIBE responses from the SPARQL endpoint."""
    chunks = [b"<a> ", b"<b> ", b"<c> .\n"]
    mw, _, _ = _build(inner=_echo_app(chunks=chunks))
    sent = await _drive(mw, _scope())

    # Expect: response.start, three response.body messages with more_body True/True/False.
    assert sent[0]["type"] == "http.response.start"
    body_msgs = [m for m in sent if m["type"] == "http.response.body"]
    assert [m["body"] for m in body_msgs] == chunks
    assert body_msgs[0]["more_body"] is True
    assert body_msgs[-1]["more_body"] is False


@pytest.mark.unit
async def test_non_http_scope_passes_through_without_emitting() -> None:
    mw, recorder, _ = _build()
    scope = {"type": "lifespan"}

    received: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, Any]) -> None:
        received.append(message)

    # Inner echo app would crash on lifespan; replace with no-op.
    async def noop(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del scope, receive, send

    mw2 = RequestObservationMiddleware(noop, bus_provider=lambda: EventBus())  # type: ignore[arg-type]
    await mw2(scope, receive, send)  # type: ignore[arg-type]
    assert recorder.events == []
