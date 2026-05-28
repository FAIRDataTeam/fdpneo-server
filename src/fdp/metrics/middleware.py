"""Per-request observer that produces :class:`RequestObserved` events.

Pure-ASGI on purpose: a :class:`starlette.middleware.base.BaseHTTPMiddleware`
buffers the response, which would break the SPARQL endpoint's streamed
CONSTRUCT/DESCRIBE payloads. This middleware sees every request, peeks
the response-start message for the status code, and publishes the
:class:`RequestObserved` event on the event bus.

The publish is fire-and-forget: failing to record a metric must never
delay or fail the response. Publishing also inherits the current
:mod:`fdp.shared.context` ContextVar via :func:`asyncio.create_task`,
so the captured subject is the one that was bound for *this* request
even if the surrounding auth middleware has already reset its token by
the time the task runs.

**Installation order**

This middleware must sit *inside* :class:`AuthenticationMiddleware` so
the :class:`RequestContext` ContextVar is bound when we snapshot it.
In FastAPI ``app.add_middleware`` registers outermost-first, so the
observer goes on the app *after* the auth middleware.

**Skip list**

We do not emit events for:

* the liveness probe (``/healthz``),
* the OpenAPI documentation tree (``/openapi.json``, ``/docs``,
  ``/redoc``),
* the metrics dashboard itself (``/metrics/*``) — would create a
  reader-feedback loop and pollute the per-resource counts.

CORS preflight (``OPTIONS``) is also skipped: it carries no caller
intent worth charting.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from fdp.metrics.events import MetricEventType, RequestObserved
from fdp.shared.context import get_current

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from fdp.shared.events import EventBus

log = structlog.get_logger(__name__)


_SKIP_PREFIXES: Final = ("/healthz", "/metrics", "/openapi.json", "/docs", "/redoc")


class RequestObservationMiddleware:
    """ASGI middleware that publishes one :class:`RequestObserved` per HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bus_provider: Callable[[], EventBus | None],
    ) -> None:
        self._app = app
        self._bus_provider = bus_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method: str = scope.get("method", "GET").upper()
        path: str = scope.get("path", "")
        if _should_skip(method, path):
            await self._app(scope, receive, send)
            return

        start = time.perf_counter()
        captured_status: int = 500

        async def _wrapped_send(message: Message) -> None:
            nonlocal captured_status
            if message["type"] == "http.response.start":
                captured_status = int(message.get("status", 500))
            await send(message)

        try:
            await self._app(scope, receive, _wrapped_send)
        finally:
            elapsed_ms = max(0, int((time.perf_counter() - start) * 1000))
            event = _build_event(scope, method, path, captured_status, elapsed_ms)
            bus = self._bus_provider()
            if bus is not None and event is not None:
                # Fire-and-forget: never block the response on metrics
                # delivery, never fail the response if delivery raises.
                asyncio.create_task(_publish_safe(bus, event))


# --- routing helpers -------------------------------------------------------


def _should_skip(method: str, path: str) -> bool:
    if method == "OPTIONS":
        return True
    for prefix in _SKIP_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _event_type_for(method: str, path: str) -> MetricEventType | None:
    """Classify the request. ``None`` means "don't record"."""
    if path == "/sparql":
        return MetricEventType.SPARQL_QUERY
    if path.startswith("/data/"):
        # /data/{id}/sparql is the per-distribution SPARQL endpoint; the
        # plain /data/{id} is a file download.
        if path.endswith("/sparql"):
            return MetricEventType.SPARQL_QUERY
        if method in ("GET", "HEAD"):
            return MetricEventType.DOWNLOAD
        return None  # the data router is read-only in v1
    # Everything else is LDP. Map HTTP verb → action.
    if method in ("GET", "HEAD"):
        return MetricEventType.VIEW
    if method in ("PUT", "POST", "PATCH"):
        return MetricEventType.MODIFY
    if method == "DELETE":
        return MetricEventType.DELETE
    return None


def _build_event(
    scope: Scope,
    method: str,
    path: str,
    status_code: int,
    latency_ms: int,
) -> RequestObserved | None:
    event_type = _event_type_for(method, path)
    if event_type is None:
        return None

    ctx = get_current()
    subject = ctx.subject if ctx is not None else None
    timestamp = ctx.request_timestamp if ctx is not None else datetime.now(UTC)
    ip = _client_ip(scope)
    ua = _header(scope, b"user-agent")
    resource_iri = _resource_iri(scope, event_type, path)

    return RequestObserved(
        timestamp=timestamp,
        event_type=event_type,
        resource_iri=resource_iri,
        method=method,
        status_code=status_code,
        latency_ms=latency_ms,
        ip=ip,
        user_agent=ua,
        subject=subject,
    )


def _resource_iri(scope: Scope, event_type: MetricEventType, path: str) -> str | None:
    """Compose the resource IRI from the request URL.

    For ``SPARQL_QUERY`` at ``/sparql`` we record no resource — the
    query targets the dataset projection, not a specific record. For
    everything else the IRI is the absolute URL.
    """
    if event_type is MetricEventType.SPARQL_QUERY and path == "/sparql":
        return None
    scheme: str = scope.get("scheme", "http")
    host = _header(scope, b"host")
    if host is None:
        server = scope.get("server")
        if isinstance(server, (tuple, list)) and len(server) >= 2:
            host_name, port = server[0], server[1]
            host = f"{host_name}:{port}" if port else str(host_name)
    if host is None:
        return path
    return f"{scheme}://{host}{path}"


def _client_ip(scope: Scope) -> str | None:
    """Read the caller's IP, honouring the first hop in X-Forwarded-For."""
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and len(client) >= 1:
        return str(client[0])
    return None


def _header(scope: Scope, name: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == name:
            try:
                return raw_value.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


# --- publish glue ----------------------------------------------------------


async def _publish_safe(bus: EventBus, event: RequestObserved) -> None:
    try:
        await bus.publish(event)
    except Exception as err:
        log.warning(
            "request_observation_publish_failed",
            event_type=event.event_type.value,
            error=repr(err),
        )


__all__ = ["RequestObservationMiddleware"]
