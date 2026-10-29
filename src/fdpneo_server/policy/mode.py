"""Access-control mode — open by default, ODRL on request (ADR-0027).

The ODRL evaluator is powerful but asks a lot of a first-time administrator:
every decision walks the ``dct:isPartOf`` chain to an Offer, the decision is
cached per subject/action/graph, and a single mis-authored policy can hide
a whole catalog. Most deployments only ever want "everyone may read, our
stewards may write". This module gives them that as the default and keeps
the full ODRL machinery one switch away.

Two modes:

* :attr:`AccessMode.OPEN` (the default) — every subject, anonymous included,
  may ``read`` every record; ``modify`` / ``delete`` / ``distribute`` are
  permitted to any subject holding one of ``writer_roles`` (``admin`` and
  ``steward`` by default). Per-record ``dct:rights`` and managed policies
  are stored and served but **not evaluated**. Publication state still
  gates visibility (ADR-0010) — a DRAFT record remains invisible to anyone
  who cannot curate it.
* :attr:`AccessMode.ODRL` — the full evaluator: inheritance walk, Offer
  evaluation, materialized decision cache (architecture §8, §9.4).

The mode is a deployment default (``FDP_ACCESS_CONTROL_MODE``) overridable
at runtime by an administrator through the ``access.control`` settings key.
The PDP receives the effective configuration per call and branches *before*
it touches the resolver or the cache, so open mode costs no store or
Postgres round-trip and never leaves rows behind in the authorization index.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from fdpneo_server.policy.model import Action, Decision, Outcome

if TYPE_CHECKING:
    from fdpneo_server.shared.context import RequestContext


class AccessMode(StrEnum):
    """Which engine answers ``authorize(subject, action, resource)``."""

    OPEN = "open"
    ODRL = "odrl"


DEFAULT_WRITER_ROLES: Final[frozenset[str]] = frozenset({"admin", "steward"})
"""Roles that may modify / delete / distribute in open mode."""


@dataclass(frozen=True)
class AccessControlConfig:
    """The effective access-control configuration for one PDP call."""

    mode: AccessMode = AccessMode.OPEN
    writer_roles: frozenset[str] = DEFAULT_WRITER_ROLES


OPEN_ACCESS: Final = AccessControlConfig(mode=AccessMode.OPEN)
"""The out-of-the-box configuration: open read, admin/steward write."""

ODRL_ACCESS: Final = AccessControlConfig(mode=AccessMode.ODRL)
"""Full ODRL evaluation; ``writer_roles`` is unused in this mode."""


def decide_open(ctx: RequestContext, action: Action, config: AccessControlConfig) -> Decision:
    """The open-mode decision — pure, no I/O, never cached.

    ``read`` is always permitted. Any other action is permitted iff the
    subject holds at least one of ``config.writer_roles``; an anonymous
    subject has no roles and is therefore denied, which the PEPs turn into
    a 401 rather than a 403.
    """
    if action is Action.READ:
        return Decision(outcome=Outcome.PERMIT, rule=None, reason="open access: read is public")
    if ctx.roles & config.writer_roles:
        return Decision(
            outcome=Outcome.PERMIT,
            rule=None,
            reason=f"open access: writer role held for {action.value}",
        )
    return Decision(
        outcome=Outcome.DENY,
        rule=None,
        reason=f"open access: {action.value} requires one of {sorted(config.writer_roles)}",
    )


__all__ = [
    "DEFAULT_WRITER_ROLES",
    "ODRL_ACCESS",
    "OPEN_ACCESS",
    "AccessControlConfig",
    "AccessMode",
    "decide_open",
]
