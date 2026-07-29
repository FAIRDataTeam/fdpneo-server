"""Metadata publication state — the value type and transition rules (ADR-0010).

A leaf module: it imports nothing from the metadata package so both the meta
builder (:mod:`fdpneo_server.metadata.meta`) and the lifecycle service/router
(:mod:`fdpneo_server.metadata.lifecycle`) can depend on it without a cycle.

State lives as ``fdp:metadataState "DRAFT|PUBLISHED|ARCHIVED"`` in a record's
``<record>/meta`` graph. New LDP-created records default to ``DRAFT``;
profile-seeded records (the root Repository, seed records) are written
``PUBLISHED`` so the deployment is anonymously usable. Only the
``POST /{record}/state`` transition API changes a record's state; an ordinary
content edit preserves it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class MetadataState(StrEnum):
    """A record's publication state."""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


DEFAULT_STATE: Final = MetadataState.DRAFT
"""State a record created through the LDP layer starts in."""

SEED_STATE: Final = MetadataState.PUBLISHED
"""State the profile applier stamps on the root Repository + seed records."""


# Allowed transitions → whether the transition requires the ``admin`` role.
# A transition not present here is forbidden (409). "Not admin-required"
# transitions are permitted to the record owner (ODRL ``modify``) or an admin
# (ADR-0010 §3).
_TRANSITIONS: Final[dict[tuple[MetadataState, MetadataState], bool]] = {
    (MetadataState.DRAFT, MetadataState.PUBLISHED): False,
    (MetadataState.PUBLISHED, MetadataState.DRAFT): False,  # unpublish
    (MetadataState.PUBLISHED, MetadataState.ARCHIVED): False,
    (MetadataState.ARCHIVED, MetadataState.DRAFT): True,  # admin-only
}


def transition_requires_admin(frm: MetadataState, to: MetadataState) -> bool | None:
    """Return whether ``frm → to`` needs admin, or ``None`` if it is forbidden.

    * ``False`` — allowed for the record owner (ODRL ``modify``) or an admin.
    * ``True``  — allowed for an admin only.
    * ``None``  — not a permitted transition (the caller raises 409).
    """
    return _TRANSITIONS.get((frm, to))


def allowed_transitions(state: MetadataState) -> tuple[MetadataState, ...]:
    """The states ``state`` may transition to next, per the state machine.

    Derived from :data:`_TRANSITIONS` (the single source of truth) — not
    hardcoded — so it stays in step with the transition rules automatically.
    Ordering is stable (definition order in ``_TRANSITIONS``). Admin-only
    transitions are included: the successor is *reachable*; whether the caller
    may take it is a per-request authorization concern (ADR-0010 §3), mirrored by
    the ADR-0022 §3 view triples which advertise the affordance, not the grant.
    """
    return tuple(to for (frm, to) in _TRANSITIONS if frm == state)


def is_visible_state(state: MetadataState | None) -> bool:
    """True iff ``state`` is publicly visible (``PUBLISHED``).

    ``DRAFT``/``ARCHIVED`` and an absent state are visible only to the record
    owner or an admin; the caller layers that check on top.
    """
    return state is MetadataState.PUBLISHED


__all__ = [
    "DEFAULT_STATE",
    "SEED_STATE",
    "MetadataState",
    "allowed_transitions",
    "is_visible_state",
    "transition_requires_admin",
]
