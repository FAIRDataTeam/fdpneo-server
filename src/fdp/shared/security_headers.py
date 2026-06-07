"""Security-response-headers middleware (security audit 2026-06-07, finding F-05).

Adds the baseline hardening headers the API was missing — HSTS, anti-sniffing,
anti-framing, a strict referrer policy, COOP, and a locked-down Content-Security-
Policy. A pure ASGI middleware (not ``BaseHTTPMiddleware``) so it sets headers on
every response — including errors and streaming bodies — without buffering.

The strict CSP (``default-src 'none'``) suits a JSON API. The interactive docs
UIs (``/docs``, ``/redoc``) load assets from a CDN and would break under it, so
they are exempted; ``/openapi.json`` (plain JSON) keeps the strict policy.
Disable the docs UIs entirely in production regardless.

``Strict-Transport-Security`` is sent unconditionally — browsers ignore it over
plain HTTP (RFC 6797), so it is harmless in dev and active once TLS terminates in
front of the app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_HSTS = "max-age=63072000; includeSubDomains"
_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
_DOCS_PREFIXES = ("/docs", "/redoc")

_STATIC_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Strict-Transport-Security", _HSTS),
)


class SecurityHeadersMiddleware:
    """Attach baseline security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        is_docs_ui = str(scope.get("path", "")).startswith(_DOCS_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _STATIC_HEADERS:
                    headers.setdefault(name, value)
                if not is_docs_ui:
                    headers.setdefault("Content-Security-Policy", _CSP)
            await send(message)

        await self._app(scope, receive, send_with_headers)


__all__ = ["SecurityHeadersMiddleware"]
