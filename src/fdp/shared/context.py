"""Per-request identity and trace context.

**Responsibilities**

* Define the immutable ``RequestContext`` that the authentication middleware
  attaches to every request and that downstream components — logging, the
  policy decision point, the metrics pipeline — read.
* Expose a ContextVar so code can find the active context without threading
  it through every function signature.

**Non-responsibilities**

* Authentication. The OIDC middleware (``identity.middleware``) is what
  *constructs* a ``RequestContext``; this module only defines the shape.
* Authorization decisions. Those live in ``policy``.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Immutable view of who is making a request and how it should be traced.

    ``subject`` is the user URI in the form ``<issuer>#<sub>`` (architecture
    §7.2); ``None`` means the caller is anonymous. ``roles`` is a frozen set
    of role names resolved from the configured IdP claim. ``request_timestamp``
    is tz-aware UTC. ``trace_id`` is the OpenTelemetry-compatible trace
    identifier used to correlate logs across modules.
    """

    subject: str | None
    roles: frozenset[str] = frozenset()
    request_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str = ""

    @property
    def is_anonymous(self) -> bool:
        """True iff there is no subject *and* no role — architecture §3.1."""
        return self.subject is None and not self.roles

    @classmethod
    def anonymous(
        cls,
        *,
        trace_id: str,
        request_timestamp: datetime | None = None,
    ) -> Self:
        """Build the canonical anonymous context for a given trace."""
        return cls(
            subject=None,
            roles=frozenset(),
            request_timestamp=request_timestamp or datetime.now(UTC),
            trace_id=trace_id,
        )


_current: ContextVar[RequestContext | None] = ContextVar(
    "fdp_request_context",
    default=None,
)


def get_current() -> RequestContext | None:
    """Return the request context bound to this async task, if any."""
    return _current.get()


def set_current(ctx: RequestContext) -> Token[RequestContext | None]:
    """Bind ``ctx`` and return a token usable with :func:`reset_current`."""
    return _current.set(ctx)


def reset_current(token: Token[RequestContext | None]) -> None:
    """Restore the ContextVar to the state captured by ``token``."""
    _current.reset(token)


@contextmanager
def bound(ctx: RequestContext) -> Generator[None, None, None]:
    """Bind ``ctx`` for the duration of the ``with`` block."""
    token = set_current(ctx)
    try:
        yield
    finally:
        reset_current(token)


__all__ = [
    "RequestContext",
    "bound",
    "get_current",
    "reset_current",
    "set_current",
]
