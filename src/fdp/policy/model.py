"""Domain model for the FDP ODRL profile.

The profile is a documented subset of ODRL (ADR-0006, architecture §8.1):

* Policy types: ``odrl:Offer`` (parsed here) and ``odrl:Agreement``
  (materialized later — not in this module).
* Rules: ``odrl:Permission`` and ``odrl:Prohibition``. Duties are not
  supported.
* Actions: ``read``, ``modify``, ``delete``, ``distribute``.
* Constraints: party identity, role membership, group membership, time
  windows.
* Conflict strategies: ``deny_wins`` (default), ``perm_wins``,
  ``invalid``.

The dataclasses here are frozen and slot-less (so subclasses share a slot
table) — they describe parsed policies, not Pydantic edge models.

**LeftOperand vocabulary**

Party and time use standard ODRL leftOperands. Role and group membership
are FDP profile extensions at a stable URI (``FDP_PROFILE_NS``) so policy
documents remain portable across deployments — they do *not* use the
per-deployment ``fdp:`` namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rdflib import Namespace

ODRL_NS = Namespace("http://www.w3.org/ns/odrl/2/")
"""Standard W3C ODRL 2.2 namespace."""

FDP_PROFILE_NS = Namespace("https://specs.fairdatapoint.org/odrl-profile#")
"""FDP ODRL profile extensions — stable across deployments."""


# --- Actions ---------------------------------------------------------------


class Action(StrEnum):
    """Actions the FDP profile recognizes (architecture §8.1)."""

    READ = "read"
    MODIFY = "modify"
    DELETE = "delete"
    DISTRIBUTE = "distribute"


ACTION_IRIS: dict[str, Action] = {str(ODRL_NS[a.value]): a for a in Action}


# --- Operators -------------------------------------------------------------


class ConstraintOperator(StrEnum):
    """Subset of ODRL comparison operators used by the FDP profile."""

    EQ = "eq"
    NEQ = "neq"
    LT = "lt"
    GT = "gt"
    LTEQ = "lteq"
    GTEQ = "gteq"


OPERATOR_IRIS: dict[str, ConstraintOperator] = {
    str(ODRL_NS[op.value]): op for op in ConstraintOperator
}


# --- Conflict strategies ---------------------------------------------------


class ConflictStrategy(StrEnum):
    """How to resolve a policy that has both a permission and a prohibition
    for the same action (architecture §8.4)."""

    DENY_WINS = "deny_wins"
    PERM_WINS = "perm_wins"
    INVALID = "invalid"


CONFLICT_IRIS: dict[str, ConflictStrategy] = {
    str(ODRL_NS.perm): ConflictStrategy.PERM_WINS,
    str(ODRL_NS.invalid): ConflictStrategy.INVALID,
    # `odrl:deny` is the explicit deny-wins choice; treated as default.
    str(ODRL_NS.deny): ConflictStrategy.DENY_WINS,
}


# --- Constraints -----------------------------------------------------------


@dataclass(frozen=True)
class Constraint:
    """Base class for the four supported constraint kinds."""

    operator: ConstraintOperator


@dataclass(frozen=True)
class PartyConstraint(Constraint):
    """The assignee's identity must satisfy ``operator`` against ``party_uri``."""

    party_uri: str


@dataclass(frozen=True)
class RoleConstraint(Constraint):
    """The assignee must (or must not) hold ``role``.

    ``eq`` is membership ("``role`` is in the subject's role set");
    ``neq`` is exclusion. Other operators are rejected at parse time.
    """

    role: str


@dataclass(frozen=True)
class GroupConstraint(Constraint):
    """The assignee must (or must not) belong to ``group``.

    Membership semantics mirror :class:`RoleConstraint`.
    """

    group: str


@dataclass(frozen=True)
class TimeConstraint(Constraint):
    """The request timestamp must satisfy ``operator`` against ``timestamp``.

    ``timestamp`` is timezone-aware UTC.
    """

    timestamp: datetime


# --- Rules -----------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """Base class for Permission and Prohibition rules."""

    action: Action
    constraints: tuple[Constraint, ...] = ()


@dataclass(frozen=True)
class Permission(Rule):
    """An ``odrl:Permission`` — grants the action when constraints match."""


@dataclass(frozen=True)
class Prohibition(Rule):
    """An ``odrl:Prohibition`` — denies the action when constraints match."""


# --- Offer -----------------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    """A parsed ``odrl:Offer``.

    ``conflict_strategy`` defaults to ``DENY_WINS`` (architecture §8.4);
    explicit ``odrl:conflict`` triples override.
    """

    iri: str
    assigner: str | None = None
    permissions: tuple[Permission, ...] = ()
    prohibitions: tuple[Prohibition, ...] = ()
    conflict_strategy: ConflictStrategy = ConflictStrategy.DENY_WINS


# --- Decision --------------------------------------------------------------


class Outcome(StrEnum):
    PERMIT = "permit"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    """Result of evaluating an Offer.

    ``rule`` is the rule that fired (``None`` if no rule applied — the
    default deny). ``reason`` is a short human-readable explanation
    suitable for an audit log line.
    """

    outcome: Outcome
    rule: Rule | None
    reason: str


__all__ = [
    "ACTION_IRIS",
    "CONFLICT_IRIS",
    "FDP_PROFILE_NS",
    "ODRL_NS",
    "OPERATOR_IRIS",
    "Action",
    "ConflictStrategy",
    "Constraint",
    "ConstraintOperator",
    "Decision",
    "GroupConstraint",
    "Offer",
    "Outcome",
    "PartyConstraint",
    "Permission",
    "Prohibition",
    "RoleConstraint",
    "Rule",
    "TimeConstraint",
]
