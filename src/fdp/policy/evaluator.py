"""Pure-function PDP for the FDP ODRL profile.

Given a parsed :class:`Offer`, a :class:`RequestContext`, and a requested
:class:`Action`, returns a :class:`Decision`.

The algorithm follows architecture §8.5:

1. Filter rules to those whose action matches the request.
2. Evaluate each rule's constraints against the context. A rule "matches"
   iff *every* one of its constraints is satisfied (logical AND).
3. Apply the offer's conflict strategy:
   * ``DENY_WINS`` (default): if any prohibition matched, deny.
   * ``PERM_WINS``: if any permission matched, permit.
   * ``INVALID``: if both kinds matched, the policy itself is invalid →
     deny with that reason.
4. If neither kind matched, deny by default.

No I/O — the evaluator is deterministic and side-effect-free.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fdp.policy.model import (
    Action,
    ConflictStrategy,
    Constraint,
    ConstraintOperator,
    Decision,
    GroupConstraint,
    Offer,
    Outcome,
    PartyConstraint,
    RoleConstraint,
    Rule,
    TimeConstraint,
)

if TYPE_CHECKING:
    from fdp.shared.context import RequestContext


def evaluate(offer: Offer, ctx: RequestContext, action: Action) -> Decision:
    """Evaluate ``offer`` against ``ctx`` for the given ``action``."""
    matching_permissions = [
        p for p in offer.permissions if p.action == action and _rule_matches(p, ctx)
    ]
    matching_prohibitions = [
        p for p in offer.prohibitions if p.action == action and _rule_matches(p, ctx)
    ]

    if (
        offer.conflict_strategy == ConflictStrategy.INVALID
        and matching_permissions
        and matching_prohibitions
    ):
        return Decision(
            outcome=Outcome.DENY,
            rule=None,
            reason="policy invalid: permission and prohibition both matched",
        )

    if offer.conflict_strategy == ConflictStrategy.PERM_WINS:
        if matching_permissions:
            return Decision(
                outcome=Outcome.PERMIT,
                rule=matching_permissions[0],
                reason="permission matched (perm_wins)",
            )
        if matching_prohibitions:
            return Decision(
                outcome=Outcome.DENY,
                rule=matching_prohibitions[0],
                reason="prohibition matched (no permission)",
            )
    else:  # DENY_WINS (the default) — and the INVALID strategy when no conflict
        if matching_prohibitions:
            return Decision(
                outcome=Outcome.DENY,
                rule=matching_prohibitions[0],
                reason="prohibition matched",
            )
        if matching_permissions:
            return Decision(
                outcome=Outcome.PERMIT,
                rule=matching_permissions[0],
                reason="permission matched",
            )

    return Decision(outcome=Outcome.DENY, rule=None, reason="no rule matched")


def _rule_matches(rule: Rule, ctx: RequestContext) -> bool:
    return all(_constraint_matches(c, ctx) for c in rule.constraints)


def _constraint_matches(constraint: Constraint, ctx: RequestContext) -> bool:
    if isinstance(constraint, PartyConstraint):
        return _eq_neq(ctx.subject or "", constraint.party_uri, constraint.operator)
    if isinstance(constraint, RoleConstraint):
        return _membership(constraint.role, ctx.roles, constraint.operator)
    if isinstance(constraint, GroupConstraint):
        return _membership(constraint.group, ctx.groups, constraint.operator)
    if isinstance(constraint, TimeConstraint):
        return _compare_datetimes(ctx.request_timestamp, constraint.timestamp, constraint.operator)
    # Unknown constraint class — fail closed.
    return False


def _eq_neq(actual: str, expected: str, op: ConstraintOperator) -> bool:
    if op == ConstraintOperator.EQ:
        return actual == expected
    if op == ConstraintOperator.NEQ:
        return actual != expected
    return False


def _membership(value: str, container: frozenset[str], op: ConstraintOperator) -> bool:
    if op == ConstraintOperator.EQ:
        return value in container
    if op == ConstraintOperator.NEQ:
        return value not in container
    return False


def _compare_datetimes(
    actual: datetime,
    expected: datetime,
    op: ConstraintOperator,
) -> bool:
    if op == ConstraintOperator.EQ:
        return actual == expected
    if op == ConstraintOperator.NEQ:
        return actual != expected
    if op == ConstraintOperator.LT:
        return actual < expected
    if op == ConstraintOperator.GT:
        return actual > expected
    if op == ConstraintOperator.LTEQ:
        return actual <= expected
    if op == ConstraintOperator.GTEQ:
        return actual >= expected
    return False


__all__ = ["evaluate"]
