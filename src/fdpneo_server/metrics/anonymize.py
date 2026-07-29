"""The anonymization boundary (architecture §11.1, ADR-0002).

The single pure function :func:`anonymize` transforms a
:class:`RequestObserved` (which carries IP, UA, and the acting subject)
into a :class:`MetricSample` (which carries none of those). Every metrics
subscriber MUST call :func:`anonymize` before persisting; the pipeline
guarantees this by routing the bus through this module rather than
giving subscribers direct access to :class:`RequestObserved`.

The function is intentionally trivial — adding logic here means adding
ways to leak identifying data, and the review bar is correspondingly
high. New fields belong on :class:`MetricSample` only when they are
provably aggregate-safe.
"""

from __future__ import annotations

from hashlib import blake2b
from typing import TYPE_CHECKING

from fdpneo_server.metrics.events import MetricSample, RequestObserved

if TYPE_CHECKING:
    from fdpneo_server.metrics.geo import GeoLookup
    from fdpneo_server.metrics.salt import SaltRotator


def anonymize(
    raw: RequestObserved,
    *,
    geo: GeoLookup,
    salt_rotator: SaltRotator,
    counting_enabled: bool,
) -> MetricSample:
    """Project ``raw`` into an aggregate-safe :class:`MetricSample`.

    * ``timestamp`` is floored to the hour.
    * ``ip`` is looked up against ``geo`` to derive country/region/city,
      then discarded.
    * ``user_agent`` participates in the visitor hash only when both IP
      and UA are present and ``counting_enabled`` is True; otherwise the
      hash is ``None``.
    * ``subject`` is dropped unconditionally.
    """
    bucket = raw.timestamp.replace(minute=0, second=0, microsecond=0)
    geo_result = geo.lookup(raw.ip)
    visitor_hash: str | None = None
    if counting_enabled and raw.ip and raw.user_agent:
        digest = blake2b(
            salt_rotator.current_salt() + raw.ip.encode("utf-8") + raw.user_agent.encode("utf-8"),
            digest_size=16,
        )
        visitor_hash = digest.hexdigest()
    return MetricSample(
        timestamp_bucket=bucket,
        event_type=raw.event_type,
        resource_iri=raw.resource_iri,
        country_code=geo_result.country_code,
        region=geo_result.region,
        city=geo_result.city,
        visitor_hash=visitor_hash,
        status_code=raw.status_code,
        latency_ms=raw.latency_ms,
    )


__all__ = ["anonymize"]
