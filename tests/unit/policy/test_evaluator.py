"""Evaluator tests — synthesize Offers in code, evaluate, assert outcome."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fdp.policy.evaluator import evaluate
from fdp.policy.model import (
    Action,
    ConflictStrategy,
    ConstraintOperator,
    Decision,
    GroupConstraint,
    Offer,
    Outcome,
    PartyConstraint,
    Permission,
    Prohibition,
    RoleConstraint,
    TimeConstraint,
)
from fdp.shared.context import RequestContext

OFFER = "https://example.org/offer/1"
ALICE = "https://idp.example/realms/fdp#alice"
BOB = "https://idp.example/realms/fdp#bob"


def _ctx(
    *,
    subject: str | None = ALICE,
    roles: frozenset[str] = frozenset(),
    groups: frozenset[str] = frozenset(),
    when: datetime | None = None,
) -> RequestContext:
    return RequestContext(
        subject=subject,
        roles=roles,
        groups=groups,
        request_timestamp=when or datetime(2026, 6, 1, tzinfo=UTC),
        trace_id="t-1",
    )


def _offer(
    *,
    permissions: tuple[Permission, ...] = (),
    prohibitions: tuple[Prohibition, ...] = (),
    conflict: ConflictStrategy = ConflictStrategy.DENY_WINS,
) -> Offer:
    return Offer(
        iri=OFFER,
        permissions=permissions,
        prohibitions=prohibitions,
        conflict_strategy=conflict,
    )


# --- default deny ----------------------------------------------------------


@pytest.mark.unit
def test_empty_offer_denies_by_default() -> None:
    decision = evaluate(_offer(), _ctx(), Action.READ)
    assert decision.outcome is Outcome.DENY
    assert decision.rule is None


@pytest.mark.unit
def test_permission_for_different_action_denies() -> None:
    decision = evaluate(
        _offer(permissions=(Permission(action=Action.MODIFY),)),
        _ctx(),
        Action.READ,
    )
    assert decision.outcome is Outcome.DENY


# --- unconstrained permission ---------------------------------------------


@pytest.mark.unit
def test_matching_permission_permits() -> None:
    decision = evaluate(
        _offer(permissions=(Permission(action=Action.READ),)),
        _ctx(),
        Action.READ,
    )
    assert decision.outcome is Outcome.PERMIT
    assert isinstance(decision.rule, Permission)


# --- party constraint ------------------------------------------------------


@pytest.mark.unit
def test_party_eq_matches() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(PartyConstraint(operator=ConstraintOperator.EQ, party_uri=ALICE),),
            ),
        )
    )
    assert evaluate(offer, _ctx(subject=ALICE), Action.READ).outcome is Outcome.PERMIT
    assert evaluate(offer, _ctx(subject=BOB), Action.READ).outcome is Outcome.DENY


@pytest.mark.unit
def test_party_neq_matches_other() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(PartyConstraint(operator=ConstraintOperator.NEQ, party_uri=ALICE),),
            ),
        )
    )
    assert evaluate(offer, _ctx(subject=BOB), Action.READ).outcome is Outcome.PERMIT
    assert evaluate(offer, _ctx(subject=ALICE), Action.READ).outcome is Outcome.DENY


# --- role / group ---------------------------------------------------------


@pytest.mark.unit
def test_role_eq_is_membership() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.MODIFY,
                constraints=(RoleConstraint(operator=ConstraintOperator.EQ, role="steward"),),
            ),
        )
    )
    assert (
        evaluate(offer, _ctx(roles=frozenset({"steward"})), Action.MODIFY).outcome is Outcome.PERMIT
    )
    assert evaluate(offer, _ctx(roles=frozenset({"viewer"})), Action.MODIFY).outcome is Outcome.DENY


@pytest.mark.unit
def test_role_neq_excludes() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(RoleConstraint(operator=ConstraintOperator.NEQ, role="banned"),),
            ),
        )
    )
    assert (
        evaluate(offer, _ctx(roles=frozenset({"steward"})), Action.READ).outcome is Outcome.PERMIT
    )
    assert evaluate(offer, _ctx(roles=frozenset({"banned"})), Action.READ).outcome is Outcome.DENY


@pytest.mark.unit
def test_group_membership() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(GroupConstraint(operator=ConstraintOperator.EQ, group="biobank-x"),),
            ),
        )
    )
    assert (
        evaluate(offer, _ctx(groups=frozenset({"biobank-x"})), Action.READ).outcome
        is Outcome.PERMIT
    )
    assert evaluate(offer, _ctx(groups=frozenset({"other"})), Action.READ).outcome is Outcome.DENY


# --- time -----------------------------------------------------------------


@pytest.mark.unit
def test_time_gteq() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(
                    TimeConstraint(
                        operator=ConstraintOperator.GTEQ,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    ),
                ),
            ),
        )
    )
    after = _ctx(when=datetime(2026, 6, 1, tzinfo=UTC))
    before = _ctx(when=datetime(2025, 12, 31, tzinfo=UTC))
    assert evaluate(offer, after, Action.READ).outcome is Outcome.PERMIT
    assert evaluate(offer, before, Action.READ).outcome is Outcome.DENY


@pytest.mark.unit
def test_time_lt() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(
                    TimeConstraint(
                        operator=ConstraintOperator.LT,
                        timestamp=datetime(2026, 12, 31, tzinfo=UTC),
                    ),
                ),
            ),
        )
    )
    assert (
        evaluate(offer, _ctx(when=datetime(2026, 6, 1, tzinfo=UTC)), Action.READ).outcome
        is Outcome.PERMIT
    )
    assert (
        evaluate(offer, _ctx(when=datetime(2027, 1, 1, tzinfo=UTC)), Action.READ).outcome
        is Outcome.DENY
    )


# --- combined constraints (AND) -------------------------------------------


@pytest.mark.unit
def test_multiple_constraints_all_must_match() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.MODIFY,
                constraints=(
                    RoleConstraint(operator=ConstraintOperator.EQ, role="steward"),
                    GroupConstraint(operator=ConstraintOperator.EQ, group="biobank-x"),
                ),
            ),
        )
    )
    only_role = _ctx(roles=frozenset({"steward"}))
    role_and_group = _ctx(roles=frozenset({"steward"}), groups=frozenset({"biobank-x"}))
    assert evaluate(offer, only_role, Action.MODIFY).outcome is Outcome.DENY
    assert evaluate(offer, role_and_group, Action.MODIFY).outcome is Outcome.PERMIT


# --- conflict resolution --------------------------------------------------


def _permit_and_prohibit_both_matching(strategy: ConflictStrategy) -> Decision:
    offer = _offer(
        permissions=(Permission(action=Action.DELETE),),
        prohibitions=(Prohibition(action=Action.DELETE),),
        conflict=strategy,
    )
    return evaluate(offer, _ctx(), Action.DELETE)


@pytest.mark.unit
def test_deny_wins_default() -> None:
    decision = _permit_and_prohibit_both_matching(ConflictStrategy.DENY_WINS)
    assert decision.outcome is Outcome.DENY
    assert isinstance(decision.rule, Prohibition)


@pytest.mark.unit
def test_perm_wins_when_configured() -> None:
    decision = _permit_and_prohibit_both_matching(ConflictStrategy.PERM_WINS)
    assert decision.outcome is Outcome.PERMIT
    assert isinstance(decision.rule, Permission)


@pytest.mark.unit
def test_invalid_strategy_denies_with_explicit_reason() -> None:
    decision = _permit_and_prohibit_both_matching(ConflictStrategy.INVALID)
    assert decision.outcome is Outcome.DENY
    assert decision.rule is None
    assert "invalid" in decision.reason


@pytest.mark.unit
def test_perm_wins_with_only_prohibition_still_denies() -> None:
    offer = _offer(
        prohibitions=(Prohibition(action=Action.DELETE),),
        conflict=ConflictStrategy.PERM_WINS,
    )
    decision = evaluate(offer, _ctx(), Action.DELETE)
    assert decision.outcome is Outcome.DENY
    assert isinstance(decision.rule, Prohibition)


# --- anonymous context ----------------------------------------------------


@pytest.mark.unit
def test_anonymous_subject_treated_as_empty_string_for_party_check() -> None:
    offer = _offer(
        permissions=(
            Permission(
                action=Action.READ,
                constraints=(PartyConstraint(operator=ConstraintOperator.EQ, party_uri=ALICE),),
            ),
        )
    )
    decision = evaluate(offer, _ctx(subject=None), Action.READ)
    assert decision.outcome is Outcome.DENY
