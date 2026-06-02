"""Concrete event types emitted by the metadata module.

The shared kernel (:mod:`fdp.shared.events`) owns the bus and the abstract
:class:`Event` base class. Each producing module defines its own event
dataclasses so consumers can subscribe by exact type without leaking
producer details across context boundaries.

Subscribers in v1:

* **Audit log** — appends to a Postgres table.
* **Metrics pipeline** — passes the event through the anonymization
  boundary (ADR-0002) before storing aggregates.

Both must work from whatever fields the dataclass exposes here; neither is
allowed to call back into the metadata module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fdp.shared.events import Event


@dataclass(frozen=True)
class RecordCreated(Event):
    """A new record was written (LDP POST, or PUT against a fresh URL).

    Carries the post-write ETag so audit subscribers can correlate without
    re-reading the record. ``subject`` is the acting principal's URI;
    ``None`` covers writes performed under an anonymous context.
    """

    record_iri: str
    subject: str | None
    etag: str
    timestamp: datetime


@dataclass(frozen=True)
class RecordModified(Event):
    """A record's content changed (LDP PUT replace / PATCH).

    Carries the post-write ETag so audit subscribers can correlate without
    re-reading the record. ``subject`` is the acting principal's URI;
    ``None`` covers writes performed under an anonymous context — the PEP
    will normally have rejected those before they reach this event, but the
    field is typed honestly because anonymity is representable in
    :class:`RequestContext`.
    """

    record_iri: str
    subject: str | None
    etag: str
    timestamp: datetime


@dataclass(frozen=True)
class RecordDeleted(Event):
    """A record was removed (LDP DELETE).

    ``etag`` is intentionally absent — there is no post-delete content to
    fingerprint. Audit subscribers can pair this with the last seen
    :class:`RecordModified` to reconstruct the pre-delete state if needed.
    """

    record_iri: str
    subject: str | None
    timestamp: datetime


@dataclass(frozen=True)
class RecordStateChanged(Event):
    """A record's publication state transitioned (POST /{record}/state; ADR-0010).

    A lifecycle change, not a content edit — there is no new content ETag to
    carry. ``from_state`` / ``to_state`` are the literal state values
    (``DRAFT`` / ``PUBLISHED`` / ``ARCHIVED``); the audit subscriber persists
    the transition.
    """

    record_iri: str
    subject: str | None
    from_state: str
    to_state: str
    timestamp: datetime


__all__ = [
    "RecordCreated",
    "RecordDeleted",
    "RecordModified",
    "RecordStateChanged",
]
