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
class RecordModified(Event):
    """A record's content changed (LDP PUT / PATCH applied successfully).

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


__all__ = ["RecordModified"]
