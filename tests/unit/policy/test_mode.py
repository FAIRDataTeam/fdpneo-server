"""Unit tests for the open access-control mode decision (ADR-0027)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fdpneo_server.policy.mode import (
    DEFAULT_WRITER_ROLES,
    ODRL_ACCESS,
    OPEN_ACCESS,
    AccessControlConfig,
    AccessMode,
    decide_open,
)
from fdpneo_server.policy.model import Action, Outcome
from fdpneo_server.shared.context import RequestContext


def _ctx(*, subject: str | None = "https://idp/alice", roles: frozenset[str] = frozenset()):
    return RequestContext(
        subject=subject,
        roles=roles,
        trace_id="t-1",
        request_timestamp=datetime(2026, 10, 29, tzinfo=UTC),
    )


@pytest.mark.unit
def test_defaults_are_open_with_admin_and_steward_writers() -> None:
    assert OPEN_ACCESS.mode is AccessMode.OPEN
    assert OPEN_ACCESS.writer_roles == DEFAULT_WRITER_ROLES == frozenset({"admin", "steward"})
    assert ODRL_ACCESS.mode is AccessMode.ODRL
    assert AccessControlConfig().mode is AccessMode.OPEN


@pytest.mark.unit
@pytest.mark.parametrize("roles", [frozenset(), frozenset({"steward"}), frozenset({"other"})])
def test_read_is_public_for_everyone(roles: frozenset[str]) -> None:
    for subject in ("https://idp/alice", None):
        decision = decide_open(_ctx(subject=subject, roles=roles), Action.READ, OPEN_ACCESS)
        assert decision.outcome is Outcome.PERMIT
        assert decision.rule is None


@pytest.mark.unit
@pytest.mark.parametrize("action", [Action.MODIFY, Action.DELETE, Action.DISTRIBUTE])
def test_writes_need_a_writer_role(action: Action) -> None:
    assert decide_open(_ctx(), action, OPEN_ACCESS).outcome is Outcome.DENY
    assert decide_open(_ctx(subject=None), action, OPEN_ACCESS).outcome is Outcome.DENY
    assert (
        decide_open(_ctx(roles=frozenset({"admin"})), action, OPEN_ACCESS).outcome is Outcome.PERMIT
    )
    assert (
        decide_open(_ctx(roles=frozenset({"steward", "x"})), action, OPEN_ACCESS).outcome
        is Outcome.PERMIT
    )


@pytest.mark.unit
def test_deny_reason_names_the_required_roles() -> None:
    decision = decide_open(_ctx(), Action.MODIFY, OPEN_ACCESS)
    assert "modify" in decision.reason
    assert "admin" in decision.reason and "steward" in decision.reason
