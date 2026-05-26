"""Metrics events at the two sides of the anonymization boundary.

Two dataclasses, one strict ordering rule (architecture §11, ADR-0002):

* :class:`RequestObserved` is what the HTTP request-observation middleware
  emits onto the event bus. It carries everything the anonymizer needs —
  IP, user agent, acting subject — and *only* the anonymizer is allowed
  to read from it.
* :class:`MetricSample` is what the metrics pipeline persists. By
  construction it has no IP, no user agent, no subject, no query text.
  The type itself is the boundary; nothing past
  :mod:`fdp.metrics.anonymize` ever sees the raw form.

Adding a field to :class:`MetricSample` that could re-identify a user is
a code-level mistake that the SHA-of-the-shape unit test in
``tests/unit/metrics/test_events.py`` catches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fdp.shared.events import Event


class MetricEventType(StrEnum):
    """Kinds of observed request the metrics pipeline counts.

    Values are stable strings persisted in Postgres; renaming any of them
    is a migration concern.
    """

    VIEW = "view"
    """``GET`` / ``HEAD`` against an LDP resource."""

    MODIFY = "modify"
    """``POST`` / ``PUT`` / ``PATCH`` against an LDP resource."""

    DELETE = "delete"
    """``DELETE`` against an LDP resource."""

    DOWNLOAD = "download"
    """Distribution download via the data provider."""

    SPARQL_QUERY = "sparql_query"
    """A request handled by the SPARQL endpoint (read or update)."""

    LOGIN = "login"
    """A successful authentication event from the identity layer."""


@dataclass(frozen=True)
class RequestObserved(Event):
    """One HTTP request observed by the request-observation middleware.

    Carries identifying envelope data (IP, UA, subject). Only
    :func:`fdp.metrics.anonymize.anonymize` is permitted to consume this;
    every other subscriber must consume :class:`MetricSample` instead.
    """

    timestamp: datetime
    event_type: MetricEventType
    resource_iri: str | None
    method: str
    status_code: int
    latency_ms: int
    ip: str | None
    user_agent: str | None
    subject: str | None


@dataclass(frozen=True)
class MetricSample:
    """An aggregate-safe metric, post-anonymization.

    All fields are either categorical, geographic, or scalar. No IP, no
    user agent, no acting subject, no query text — by construction.
    """

    timestamp_bucket: datetime
    """The observation's hour, floored to :01:00:00."""

    event_type: MetricEventType
    resource_iri: str | None
    country_code: str | None
    """ISO 3166-1 alpha-2; ``None`` when geolocation produced no answer."""

    region: str | None
    city: str | None
    visitor_hash: str | None
    """blake2b of (daily salt + IP + UA). ``None`` when counting is
    disabled or either IP / UA is absent."""

    status_code: int
    latency_ms: int


__all__ = [
    "MetricEventType",
    "MetricSample",
    "RequestObserved",
]
