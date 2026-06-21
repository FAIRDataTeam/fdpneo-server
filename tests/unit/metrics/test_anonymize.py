"""Unit tests for :mod:`fdp.metrics.anonymize`.

The anonymizer is the privacy boundary: the tests here verify both the
positive contract (geo + visitor-hash derivation work) and the negative
contract (no identifying field reaches the output).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from fdp.metrics.anonymize import anonymize
from fdp.metrics.events import MetricEventType, RequestObserved
from fdp.metrics.geo import GeoLookup, GeoResult, NullGeoLookup
from fdp.metrics.salt import SaltRotator

NOW = datetime(2026, 6, 1, 12, 34, 56, tzinfo=UTC)


@dataclass
class _FixedGeo:
    """Returns a configured GeoResult regardless of IP."""

    result: GeoResult

    def lookup(self, ip: str | None) -> GeoResult:
        del ip
        return self.result

    def close(self) -> None:
        return None


class _FakeMonotonic:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _raw(
    *,
    ip: str | None = "203.0.113.5",
    user_agent: str | None = "Mozilla/5.0",
    subject: str | None = "https://idp/alice",
) -> RequestObserved:
    return RequestObserved(
        timestamp=NOW,
        event_type=MetricEventType.VIEW,
        resource_iri="https://example.org/r1",
        method="GET",
        status_code=200,
        latency_ms=42,
        ip=ip,
        user_agent=user_agent,
        subject=subject,
    )


def _rotator(clock: _FakeMonotonic | None = None) -> SaltRotator:
    return SaltRotator(clock=clock or _FakeMonotonic())


def _geo_us() -> GeoLookup:
    return _FixedGeo(GeoResult(country_code="US", region="California", city="San Francisco"))


# --- shape / boundary -------------------------------------------------------


@pytest.mark.unit
def test_anonymize_returns_metric_sample_with_geo_fields() -> None:
    sample = anonymize(
        _raw(),
        geo=_geo_us(),
        salt_rotator=_rotator(),
        counting_enabled=True,
    )
    assert sample.country_code == "US"
    assert sample.region == "California"
    assert sample.city == "San Francisco"
    assert sample.event_type is MetricEventType.VIEW
    assert sample.resource_iri == "https://example.org/r1"
    assert sample.status_code == 200
    assert sample.latency_ms == 42


@pytest.mark.unit
def test_anonymize_floors_timestamp_to_the_hour() -> None:
    sample = anonymize(
        _raw(),
        geo=NullGeoLookup(),
        salt_rotator=_rotator(),
        counting_enabled=False,
    )
    expected = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert sample.timestamp_bucket == expected


# --- visitor-hash semantics -------------------------------------------------


@pytest.mark.unit
def test_visitor_hash_is_populated_when_counting_enabled_and_envelope_present() -> None:
    sample = anonymize(
        _raw(),
        geo=NullGeoLookup(),
        salt_rotator=_rotator(),
        counting_enabled=True,
    )
    assert sample.visitor_hash is not None
    assert len(sample.visitor_hash) == 32  # blake2b-128 hex


@pytest.mark.unit
def test_visitor_hash_is_none_when_counting_disabled() -> None:
    sample = anonymize(
        _raw(),
        geo=NullGeoLookup(),
        salt_rotator=_rotator(),
        counting_enabled=False,
    )
    assert sample.visitor_hash is None


@pytest.mark.unit
def test_visitor_hash_is_none_without_ip() -> None:
    sample = anonymize(
        _raw(ip=None),
        geo=NullGeoLookup(),
        salt_rotator=_rotator(),
        counting_enabled=True,
    )
    assert sample.visitor_hash is None


@pytest.mark.unit
def test_visitor_hash_is_none_without_user_agent() -> None:
    sample = anonymize(
        _raw(user_agent=None),
        geo=NullGeoLookup(),
        salt_rotator=_rotator(),
        counting_enabled=True,
    )
    assert sample.visitor_hash is None


@pytest.mark.unit
def test_same_envelope_hashes_to_same_value_within_a_day() -> None:
    clock = _FakeMonotonic()
    rotator = _rotator(clock)
    a = anonymize(_raw(), geo=NullGeoLookup(), salt_rotator=rotator, counting_enabled=True)
    clock.advance(seconds=12 * 60 * 60)  # 12h later, same window
    b = anonymize(_raw(), geo=NullGeoLookup(), salt_rotator=rotator, counting_enabled=True)
    assert a.visitor_hash == b.visitor_hash


@pytest.mark.unit
def test_same_envelope_hashes_to_different_value_across_day_boundary() -> None:
    clock = _FakeMonotonic()
    rotator = _rotator(clock)
    a = anonymize(_raw(), geo=NullGeoLookup(), salt_rotator=rotator, counting_enabled=True)
    clock.advance(seconds=24 * 60 * 60)  # forces rotation
    b = anonymize(_raw(), geo=NullGeoLookup(), salt_rotator=rotator, counting_enabled=True)
    assert a.visitor_hash != b.visitor_hash


@pytest.mark.unit
def test_different_envelopes_hash_differently() -> None:
    rotator = _rotator()
    a = anonymize(
        _raw(ip="203.0.113.1", user_agent="UA-A"),
        geo=NullGeoLookup(),
        salt_rotator=rotator,
        counting_enabled=True,
    )
    b = anonymize(
        _raw(ip="203.0.113.2", user_agent="UA-A"),
        geo=NullGeoLookup(),
        salt_rotator=rotator,
        counting_enabled=True,
    )
    c = anonymize(
        _raw(ip="203.0.113.1", user_agent="UA-B"),
        geo=NullGeoLookup(),
        salt_rotator=rotator,
        counting_enabled=True,
    )
    assert len({a.visitor_hash, b.visitor_hash, c.visitor_hash}) == 3


# --- never-leak property ---------------------------------------------------


@pytest.mark.unit
def test_subject_is_never_in_sample_string_representation() -> None:
    """Subject must not leak into the output, even via repr() coincidence."""
    raw = _raw(subject="https://idp/alice-secret-handle")
    sample = anonymize(raw, geo=_geo_us(), salt_rotator=_rotator(), counting_enabled=True)
    assert "alice-secret-handle" not in repr(sample)


@pytest.mark.unit
def test_ip_is_never_in_sample_string_representation() -> None:
    raw = _raw(ip="203.0.113.42")
    sample = anonymize(raw, geo=_geo_us(), salt_rotator=_rotator(), counting_enabled=True)
    assert "203.0.113.42" not in repr(sample)


@pytest.mark.unit
def test_user_agent_is_never_in_sample_string_representation() -> None:
    raw = _raw(user_agent="MyDistinctiveAgent/1.0")
    sample = anonymize(raw, geo=_geo_us(), salt_rotator=_rotator(), counting_enabled=True)
    assert "MyDistinctiveAgent" not in repr(sample)
