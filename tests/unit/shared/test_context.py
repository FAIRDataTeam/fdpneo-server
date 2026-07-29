"""Tests for ``fdpneo_server.shared.context``."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from fdpneo_server.shared.context import RequestContext, bound, get_current


@pytest.mark.unit
def test_request_context_is_frozen() -> None:
    ctx = RequestContext.anonymous(trace_id="t-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.subject = "https://idp.example/realms/fdp#alice"  # type: ignore[misc]


@pytest.mark.unit
def test_anonymous_factory_is_anonymous() -> None:
    ctx = RequestContext.anonymous(trace_id="t-1")
    assert ctx.is_anonymous is True
    assert ctx.subject is None
    assert ctx.roles == frozenset()
    assert ctx.trace_id == "t-1"
    assert ctx.request_timestamp.tzinfo is not None


@pytest.mark.unit
def test_authenticated_with_no_roles_is_not_anonymous() -> None:
    ctx = RequestContext(
        subject="https://idp.example/realms/fdp#alice",
        roles=frozenset(),
        request_timestamp=datetime.now(UTC),
        trace_id="t-2",
    )
    assert ctx.is_anonymous is False


@pytest.mark.unit
def test_anonymous_with_explicit_timestamp_uses_it() -> None:
    when = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    ctx = RequestContext.anonymous(trace_id="t-3", request_timestamp=when)
    assert ctx.request_timestamp == when


@pytest.mark.unit
def test_get_current_returns_none_outside_bound() -> None:
    assert get_current() is None


@pytest.mark.unit
def test_bound_sets_and_restores() -> None:
    ctx = RequestContext.anonymous(trace_id="t-outer")
    assert get_current() is None
    with bound(ctx):
        assert get_current() is ctx
    assert get_current() is None


@pytest.mark.unit
def test_bound_nests_and_restores_outer() -> None:
    outer = RequestContext.anonymous(trace_id="outer")
    inner = RequestContext.anonymous(trace_id="inner")
    with bound(outer):
        assert get_current() is outer
        with bound(inner):
            assert get_current() is inner
        assert get_current() is outer
    assert get_current() is None
