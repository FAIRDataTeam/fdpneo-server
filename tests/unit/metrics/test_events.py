"""Unit tests for :mod:`fdpneo_server.metrics.events`.

The most important test here is :func:`test_metric_sample_has_no_identifying_fields`:
it asserts the *shape* of the post-anonymization sample. Adding an IP /
UA / subject / query-text field to :class:`MetricSample` would be a
boundary violation; this test catches it.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from fdpneo_server.metrics.events import MetricEventType, MetricSample, RequestObserved
from fdpneo_server.shared.events import Event

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_request_observed_is_an_event_subclass() -> None:
    assert issubclass(RequestObserved, Event)


@pytest.mark.unit
def test_request_observed_is_frozen() -> None:
    evt = RequestObserved(
        timestamp=NOW,
        event_type=MetricEventType.VIEW,
        resource_iri="https://example.org/r1",
        method="GET",
        status_code=200,
        latency_ms=12,
        ip="203.0.113.5",
        user_agent="Mozilla/5.0",
        subject=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        evt.ip = "203.0.113.6"  # type: ignore[misc]


@pytest.mark.unit
def test_metric_sample_is_frozen() -> None:
    sample = MetricSample(
        timestamp_bucket=NOW,
        event_type=MetricEventType.VIEW,
        resource_iri=None,
        country_code=None,
        region=None,
        city=None,
        visitor_hash=None,
        status_code=200,
        latency_ms=10,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.status_code = 500  # type: ignore[misc]


@pytest.mark.unit
def test_metric_sample_has_no_identifying_fields() -> None:
    """Boundary guard — adding any of these would re-introduce PII risk."""
    forbidden = {"ip", "user_agent", "subject", "query", "query_text", "referrer"}
    field_names = {f.name for f in dataclasses.fields(MetricSample)}
    leaks = field_names & forbidden
    assert not leaks, f"MetricSample exposes identifying field(s): {leaks!r}"


@pytest.mark.unit
def test_metric_event_type_values_are_stable_strings() -> None:
    # These values are persisted in Postgres; renames are migrations.
    assert MetricEventType.VIEW.value == "view"
    assert MetricEventType.MODIFY.value == "modify"
    assert MetricEventType.DELETE.value == "delete"
    assert MetricEventType.DOWNLOAD.value == "download"
    assert MetricEventType.SPARQL_QUERY.value == "sparql_query"
    assert MetricEventType.LOGIN.value == "login"
