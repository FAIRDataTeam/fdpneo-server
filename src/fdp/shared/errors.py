"""Application error hierarchy and FastAPI exception handler.

**Responsibilities**

* Define ``FDPError`` and concrete subclasses that the rest of the codebase
  raises for domain failures (not found, forbidden, conflict, schema
  violation, policy violation).
* Render any ``FDPError`` to the JSON envelope documented in architecture
  §14.3 — stable ``code``, human-readable ``message``, ``docs_url`` pointing
  at the documentation for that code, and an optional ``details`` slot.

**Non-responsibilities**

* Translating framework exceptions (Pydantic ``ValidationError``, FastAPI's
  ``HTTPException``) into the same envelope. That belongs in the API layer
  when the API surface is built.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from fdp.shared.context import get_current

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DOCS_BASE: Final = "https://specs.fairdatapoint.org/errors#"

log = structlog.get_logger(__name__)


class FDPError(Exception):
    """Base class for every domain error the server raises.

    Subclasses set ``code``, ``http_status``, and ``docs_url`` as class
    attributes. Instances carry the human-readable ``message`` and an
    optional ``details`` payload (for example, the SHACL violation report).
    """

    code: ClassVar[str]
    http_status: ClassVar[int]
    docs_url: ClassVar[str]

    def __init__(
        self,
        message: str,
        *,
        details: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        # Advisory HTTP headers to attach to the error response — e.g. ``Allow``
        # on a 405 (RFC 7231 §6.5.5) or ``Accept-Post`` on a 415 to a container
        # (LDP §4.2.1). Mutable so a raising site can augment it after the fact.
        self.headers: dict[str, str] = dict(headers) if headers else {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("code", "http_status", "docs_url"):
            if attr not in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} must define class attribute {attr!r} "
                    "to be a usable FDPError subclass."
                )


class Unauthenticated(FDPError):
    """Raised when a request requires authentication that wasn't provided.

    Maps to HTTP 401. ``Forbidden`` (403) is reserved for the policy layer:
    *anonymous* is the absence of authentication, *unauthorized* is a denied
    decision against an authenticated subject.
    """

    code = "fdp.unauthenticated"
    http_status = 401
    docs_url = _DOCS_BASE + "fdp.unauthenticated"


class NotFound(FDPError):
    code = "fdp.not_found"
    http_status = 404
    docs_url = _DOCS_BASE + "fdp.not_found"


class Forbidden(FDPError):
    code = "fdp.forbidden"
    http_status = 403
    docs_url = _DOCS_BASE + "fdp.forbidden"


class Conflict(FDPError):
    code = "fdp.conflict"
    http_status = 409
    docs_url = _DOCS_BASE + "fdp.conflict"


class SchemaViolation(FDPError):
    """Raised when a graph fails SHACL validation. ``details`` carries the report."""

    code = "fdp.schema_violation"
    http_status = 422
    docs_url = _DOCS_BASE + "fdp.schema_violation"


class PolicyViolation(FDPError):
    """Raised when an ODRL policy denies the requested action."""

    code = "fdp.policy_violation"
    http_status = 403
    docs_url = _DOCS_BASE + "fdp.policy_violation"


class BadRequest(FDPError):
    """Raised when a client sends an unparseable or semantically invalid request."""

    code = "fdp.bad_request"
    http_status = 400
    docs_url = _DOCS_BASE + "fdp.bad_request"


class AmbiguousSubject(BadRequest):
    """Raised when a write body has no unambiguous primary subject.

    ADR-0016 §1 / ADR-0014 §3: a write must address the record as ``<>`` / its
    canonical IRI, or contain exactly one typed IRI subject (which is rebound to
    the canonical IRI). Zero typed subjects, several, or a blank-node-only body
    is rejected rather than stored under a canonical graph key it never mentions.
    """

    code = "fdp.ambiguous_subject"
    http_status = 400
    docs_url = _DOCS_BASE + "fdp.ambiguous_subject"


class NotAcceptable(FDPError):
    """Raised when none of the client's Accept media types is supported."""

    code = "fdp.not_acceptable"
    http_status = 406
    docs_url = _DOCS_BASE + "fdp.not_acceptable"


class PreconditionFailed(FDPError):
    """Raised when an ``If-Match`` ETag does not match the current resource."""

    code = "fdp.precondition_failed"
    http_status = 412
    docs_url = _DOCS_BASE + "fdp.precondition_failed"


class PreconditionRequired(FDPError):
    """Raised when ``If-Match`` is required but absent on a conditional write."""

    code = "fdp.precondition_required"
    http_status = 428
    docs_url = _DOCS_BASE + "fdp.precondition_required"


class UnsupportedMediaType(FDPError):
    """Raised when a request body's Content-Type isn't supported on this route."""

    code = "fdp.unsupported_media_type"
    http_status = 415
    docs_url = _DOCS_BASE + "fdp.unsupported_media_type"


class MethodNotAllowed(FDPError):
    """Raised when the HTTP method isn't valid for this resource type."""

    code = "fdp.method_not_allowed"
    http_status = 405
    docs_url = _DOCS_BASE + "fdp.method_not_allowed"


class ServiceUnavailable(FDPError):
    """Raised when an optional capability is requested but not configured.

    Distinct from :class:`UpstreamError`: the dependency isn't down, it was
    never wired up (e.g. the IdP user-management facade with no admin client).
    """

    code = "fdp.service_unavailable"
    http_status = 503
    docs_url = _DOCS_BASE + "fdp.service_unavailable"


class UpstreamError(FDPError):
    """Raised when a required upstream dependency (e.g. the IdP Admin API) fails."""

    code = "fdp.upstream_error"
    http_status = 502
    docs_url = _DOCS_BASE + "fdp.upstream_error"


class GatewayTimeout(FDPError):
    """Raised when an upstream call (e.g. a SPARQL query to the store) times out."""

    code = "fdp.gateway_timeout"
    http_status = 504
    docs_url = _DOCS_BASE + "fdp.gateway_timeout"


class PayloadTooLarge(FDPError):
    """Raised when a request body exceeds the configured maximum (audit R-02)."""

    code = "fdp.payload_too_large"
    http_status = 413
    docs_url = _DOCS_BASE + "fdp.payload_too_large"


class TooManyRequests(FDPError):
    """Raised when a client exceeds the configured request rate (audit R-02)."""

    code = "fdp.too_many_requests"
    http_status = 429
    docs_url = _DOCS_BASE + "fdp.too_many_requests"


class InternalError(FDPError):
    """A generic 500 for an unexpected (non-domain) error — no internals exposed."""

    code = "fdp.internal_error"
    http_status = 500
    docs_url = _DOCS_BASE + "fdp.internal_error"


def error_envelope(exc: FDPError) -> dict[str, object]:
    """The documented error JSON: ``{code, message, docs_url, details}``.

    Single source of truth for the error-response body. ASGI middlewares that
    must emit an error before the FastAPI handler chain runs (auth, rate limit,
    body-size) build their bytes from this so the envelope shape never drifts.
    """
    return {
        "code": exc.code,
        "message": exc.message,
        "docs_url": exc.docs_url,
        "details": jsonable_encoder(exc.details) if exc.details is not None else None,
    }


async def fdp_error_handler(_request: Request, exc: FDPError) -> JSONResponse:
    """Render an ``FDPError`` as the documented JSON envelope."""
    ctx = get_current()
    log.warning(
        "fdp_error",
        code=exc.code,
        http_status=exc.http_status,
        message=exc.message,
        trace_id=ctx.trace_id if ctx is not None else None,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc),
        headers=exc.headers or None,
    )


class CatchAllExceptionMiddleware:
    """Render *any* exception that escapes the routes as the FDP error envelope.

    Registered inside CORS so the envelope (and CORS headers) reach the client
    for exceptions raised in the outer middleware layer or otherwise unhandled —
    closing the gap where an unexpected error became a bare ``500`` with no
    envelope/CORS (audit R-08). ``FDPError`` keeps its own status; anything else
    becomes a generic ``500`` with the stack logged, never returned.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, receive, guarded_send)
        except FDPError as exc:
            if started:
                raise
            await _send_envelope(send, exc.http_status, error_envelope(exc), exc.headers)
        except Exception as exc:
            ctx = get_current()
            log.error(
                "unhandled_exception",
                error=repr(exc),
                trace_id=ctx.trace_id if ctx is not None else None,
                exc_info=exc,
            )
            if started:
                raise
            err = InternalError("an internal error occurred")
            await _send_envelope(send, err.http_status, error_envelope(err))


async def _send_envelope(
    send: Send, status: int, body: dict[str, object], headers: dict[str, str] | None = None
) -> None:
    payload = json.dumps(body).encode("utf-8")
    raw_headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    for name, value in (headers or {}).items():
        raw_headers.append((name.encode("latin-1"), value.encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": raw_headers,
        }
    )
    await send({"type": "http.response.body", "body": payload})


def register_exception_handlers(app: FastAPI) -> None:
    """Mount the FDP error handler on ``app``.

    Called once from the application factory; idempotent at the FastAPI level
    (re-registering the same exception type simply replaces the handler).
    """
    app.add_exception_handler(FDPError, fdp_error_handler)  # type: ignore[arg-type]


__all__ = [
    "AmbiguousSubject",
    "BadRequest",
    "CatchAllExceptionMiddleware",
    "Conflict",
    "FDPError",
    "Forbidden",
    "GatewayTimeout",
    "InternalError",
    "MethodNotAllowed",
    "NotAcceptable",
    "NotFound",
    "PayloadTooLarge",
    "PolicyViolation",
    "PreconditionFailed",
    "PreconditionRequired",
    "SchemaViolation",
    "ServiceUnavailable",
    "TooManyRequests",
    "Unauthenticated",
    "UnsupportedMediaType",
    "UpstreamError",
    "error_envelope",
    "fdp_error_handler",
    "register_exception_handlers",
]
