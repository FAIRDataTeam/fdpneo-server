"""FastAPI dependencies that surface the current :class:`RequestContext`."""

from __future__ import annotations

from fastapi import Depends

from fdp.shared.context import RequestContext, get_current
from fdp.shared.errors import Unauthenticated


def current_context() -> RequestContext:
    """Return the active :class:`RequestContext`.

    The authentication middleware always sets a context (anonymous when no
    valid token is present), so a missing context indicates a programming
    error — typically a test app that forgot to install the middleware.
    """
    ctx = get_current()
    if ctx is None:
        raise RuntimeError("No RequestContext bound. AuthenticationMiddleware must be installed.")
    return ctx


def require_auth(ctx: RequestContext = Depends(current_context)) -> RequestContext:
    """Yield the context iff it represents an authenticated subject.

    Anonymous contexts are rejected with :class:`Unauthenticated` (401).
    Authorization decisions are the policy layer's job — having zero roles
    does not by itself disqualify an authenticated user here.
    """
    if ctx.is_anonymous:
        raise Unauthenticated("authentication required")
    return ctx


__all__ = ["current_context", "require_auth"]
