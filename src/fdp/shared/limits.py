"""Request-limit middlewares — app-level DoS defense (security audit R-02).

Two pure-ASGI middlewares, both fail-open-safe and dependency-free:

* :class:`RateLimitMiddleware` — a per-instance, in-memory fixed-window limiter
  keyed by client IP; returns ``429`` with ``Retry-After`` when a client exceeds
  the configured rate. This is *defense-in-depth*; the authoritative control for
  a multi-instance/hospital deployment is the reverse proxy / ingress.
* :class:`BodySizeLimitMiddleware` — rejects oversize request bodies with ``413``,
  both on a declared ``Content-Length`` (fast path) and by counting streamed
  bytes (so a missing/lying length can't bypass the cap).

Both emit the standard FDP error envelope so clients see a consistent shape.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Final

from fdp.shared.errors import PayloadTooLarge, TooManyRequests

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Cap the limiter's key table so a flood of unique source IPs can't grow it
# without bound; stale (previous-window) keys are purged when the window rolls.
_MAX_TRACKED_KEYS: Final = 50_000


def _client_ip(scope: Scope, *, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == b"x-forwarded-for" and raw_value:
                # Leftmost entry is the original client (proxy must sanitize XFF).
                return raw_value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


async def _send_envelope(send: Send, error: TooManyRequests | PayloadTooLarge, *, extra_headers=()):
    body = json.dumps(
        {"code": error.code, "message": error.message, "docs_url": error.docs_url, "details": None}
    ).encode("utf-8")
    headers = [(b"content-type", b"application/json"), *extra_headers]
    await send({"type": "http.response.start", "status": error.http_status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    """Fixed-window per-IP rate limiter. Returns ``429`` over the threshold."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        requests_per_window: int,
        window_seconds: int,
        trust_forwarded_for: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._app = app
        self._limit = requests_per_window
        self._window = max(window_seconds, 1)
        self._trust_xff = trust_forwarded_for
        self._clock = clock or time.monotonic
        self._counts: dict[str, list[int]] = {}  # key -> [window_id, count]
        self._current_window = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        now = self._clock()
        window_id = int(now // self._window)
        allowed, retry_after = self._check(
            _client_ip(scope, trust_forwarded_for=self._trust_xff), window_id
        )
        if not allowed:
            await _send_envelope(
                send,
                TooManyRequests("request rate limit exceeded; retry later"),
                extra_headers=((b"retry-after", str(retry_after).encode()),),
            )
            return
        await self._app(scope, receive, send)

    def _check(self, key: str, window_id: int) -> tuple[bool, int]:
        # No await between read and write → atomic within the event loop.
        if window_id != self._current_window:
            self._purge(window_id)
        entry = self._counts.get(key)
        if entry is None or entry[0] != window_id:
            self._counts[key] = [window_id, 1]
            return True, 0
        entry[1] += 1
        if entry[1] > self._limit:
            return False, self._window
        return True, 0

    def _purge(self, window_id: int) -> None:
        self._current_window = window_id
        stale = [k for k, v in self._counts.items() if v[0] != window_id]
        for k in stale:
            del self._counts[k]
        if len(self._counts) > _MAX_TRACKED_KEYS:
            self._counts.clear()


class _PayloadTooLargeError(Exception):
    """Internal: raised from the receive-wrapper when the body cap is exceeded."""


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with ``413``."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Fast path: a declared Content-Length over the cap is rejected up front.
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == b"content-length":
                try:
                    if int(raw_value) > self._max:
                        await _reject_too_large(send)
                        return
                except ValueError:
                    pass
                break

        received = 0
        started = False

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    raise _PayloadTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, capped_receive, guarded_send)
        except _PayloadTooLargeError:
            if started:
                raise  # response already began; cannot rewrite the status
            await _reject_too_large(send)


async def _reject_too_large(send: Send) -> None:
    await _send_envelope(send, PayloadTooLarge("request body exceeds the maximum allowed size"))


__all__ = ["BodySizeLimitMiddleware", "RateLimitMiddleware"]
