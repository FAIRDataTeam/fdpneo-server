"""Unit tests for ``fdpneo_server.identity.deps``."""

from __future__ import annotations

import pytest

from fdpneo_server.identity.deps import current_context, require_auth
from fdpneo_server.shared.context import RequestContext, bound
from fdpneo_server.shared.errors import Unauthenticated


@pytest.mark.unit
def test_current_context_returns_bound_context() -> None:
    anon = RequestContext.anonymous(trace_id="t-1")
    with bound(anon):
        assert current_context() is anon


@pytest.mark.unit
def test_current_context_raises_when_no_middleware_installed() -> None:
    with pytest.raises(RuntimeError, match="AuthenticationMiddleware"):
        current_context()


@pytest.mark.unit
def test_require_auth_rejects_anonymous_context() -> None:
    anon = RequestContext.anonymous(trace_id="t-2")
    with pytest.raises(Unauthenticated):
        require_auth(anon)


@pytest.mark.unit
def test_require_auth_returns_authenticated_context() -> None:
    ctx = RequestContext(
        subject="http://idp.local/realms/fdp#alice",
        roles=frozenset({"steward"}),
        trace_id="t-3",
    )
    assert require_auth(ctx) is ctx
