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

from typing import ClassVar, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from fdp.shared.context import get_current

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

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

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
    body = {
        "code": exc.code,
        "message": exc.message,
        "docs_url": exc.docs_url,
        "details": jsonable_encoder(exc.details) if exc.details is not None else None,
    }
    return JSONResponse(status_code=exc.http_status, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """Mount the FDP error handler on ``app``.

    Called once from the application factory; idempotent at the FastAPI level
    (re-registering the same exception type simply replaces the handler).
    """
    app.add_exception_handler(FDPError, fdp_error_handler)  # type: ignore[arg-type]


__all__ = [
    "BadRequest",
    "Conflict",
    "FDPError",
    "Forbidden",
    "MethodNotAllowed",
    "NotAcceptable",
    "NotFound",
    "PolicyViolation",
    "PreconditionFailed",
    "PreconditionRequired",
    "SchemaViolation",
    "Unauthenticated",
    "UnsupportedMediaType",
    "fdp_error_handler",
    "register_exception_handlers",
]
