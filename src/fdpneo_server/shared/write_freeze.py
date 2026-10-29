"""A process-wide write freeze for consistent online backups (ADR-0029 §6).

While an admin-triggered full dump (or full restore) runs over HTTP, the server
keeps answering reads but refuses writes with ``503`` and ``Retry-After``, so
the triple store and the Postgres state tables are captured at one consistent
point. The freeze is a counter, so overlapping holders compose.

"Write" means any method other than ``GET``/``HEAD``/``OPTIONS`` — except the
few ``POST`` endpoints that only read (SPARQL query, search), which stay open.

Cross-cutting by nature (every bounded context's write endpoints are affected),
hence the shared kernel. Single-process: a multi-replica deployment takes full
backups with the CLI while the servers are stopped (``deploy/stack/backup.sh``).
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import TYPE_CHECKING, Final

from fdpneo_server.shared.errors import ServiceUnavailable, error_envelope
from fdpneo_server.shared.reserved import RESERVED_API_PATH

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import ASGIApp, Receive, Scope, Send

_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_READ_ONLY_POSTS: Final = re.compile(
    rf"^{re.escape(RESERVED_API_PATH)}/(?:sparql|search|data/[^/]+/sparql)/?$"
)
RETRY_AFTER_SECONDS: Final = 30


class WriteFreeze:
    """Counts active holders; writes are refused while any holder is active."""

    def __init__(self) -> None:
        self._holders = 0
        self._reason: str | None = None

    @property
    def active(self) -> bool:
        return self._holders > 0

    @property
    def reason(self) -> str | None:
        return self._reason if self.active else None

    @contextlib.asynccontextmanager
    async def hold(self, reason: str) -> AsyncGenerator[None]:
        """Freeze writes for the duration of the ``async with`` block."""
        self._holders += 1
        self._reason = reason
        try:
            yield
        finally:
            self._holders -= 1


def is_write_request(method: str, path: str) -> bool:
    """True when a request would change state (and must wait out a freeze)."""
    if method.upper() in _SAFE_METHODS:
        return False
    return not (method.upper() == "POST" and _READ_ONLY_POSTS.match(path))


class WriteFreezeMiddleware:
    """Answer write requests with ``503`` + ``Retry-After`` while frozen."""

    def __init__(self, app: ASGIApp, *, freeze: WriteFreeze) -> None:
        self._app = app
        self._freeze = freeze

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self._freeze.active
            and is_write_request(str(scope.get("method", "GET")), str(scope.get("path", "")))
        ):
            error = ServiceUnavailable(
                f"writes are paused while {self._freeze.reason or 'maintenance'} runs; "
                "retry shortly",
                details={"retry_after_seconds": RETRY_AFTER_SECONDS},
            )
            body = json.dumps(error_envelope(error)).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": error.http_status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(RETRY_AFTER_SECONDS).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


__all__ = ["RETRY_AFTER_SECONDS", "WriteFreeze", "WriteFreezeMiddleware", "is_write_request"]
