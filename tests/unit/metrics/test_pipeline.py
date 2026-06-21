"""Unit tests for :class:`MetricsPipeline`.

The pipeline's contract is narrow: subscribe to ``RequestObserved``,
run it through ``anonymize``, persist the resulting ``MetricSample``.
Tests use a fake session factory so the boundary's behavior is
verified without I/O.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from fdp.metrics.events import MetricEventType, MetricSample, RequestObserved
from fdp.metrics.geo import GeoResult
from fdp.metrics.pipeline import MetricsPipeline
from fdp.metrics.salt import SaltRotator
from fdp.shared.events import EventBus

NOW = datetime(2026, 6, 1, 12, 34, 56, tzinfo=UTC)


@dataclass
class _FakeGeo:
    result: GeoResult = field(
        default_factory=lambda: GeoResult(country_code="US", region="CA", city="SF")
    )

    def lookup(self, ip: str | None) -> GeoResult:
        del ip
        return self.result

    def close(self) -> None:
        return None


class _FakeSession:
    """Records what the pipeline persisted, without touching SQL."""

    def __init__(self, captured: list[MetricSample]) -> None:
        self._captured = captured
        self.committed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def add(self, obj: Any) -> None:
        # The repository builds a MetricsRaw ORM instance from the sample;
        # rebuild a MetricSample from the row fields so tests can assert
        # over the boundary's output without depending on the ORM type.
        sample = MetricSample(
            timestamp_bucket=obj.bucket,
            event_type=MetricEventType(obj.event_type),
            resource_iri=obj.resource_iri,
            country_code=obj.country_code,
            region=obj.region,
            city=obj.city,
            visitor_hash=obj.visitor_hash,
            status_code=obj.status_code,
            latency_ms=obj.latency_ms,
        )
        self._captured.append(sample)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.captured: list[MetricSample] = []

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.captured)


def _raw(
    *,
    event_type: MetricEventType = MetricEventType.VIEW,
    resource_iri: str | None = "https://example.org/r1",
    subject: str | None = "https://idp/alice",
    ip: str | None = "203.0.113.5",
    ua: str | None = "Mozilla/5.0",
) -> RequestObserved:
    return RequestObserved(
        timestamp=NOW,
        event_type=event_type,
        resource_iri=resource_iri,
        method="GET",
        status_code=200,
        latency_ms=42,
        ip=ip,
        user_agent=ua,
        subject=subject,
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def factory() -> _FakeSessionFactory:
    return _FakeSessionFactory()


@pytest.fixture
def pipeline_factory(factory: _FakeSessionFactory) -> Iterator[Any]:
    """Returns a builder so each test can opt into the enabled flags it needs."""

    def _build(*, enabled: bool = True, counting_enabled: bool = True) -> MetricsPipeline:
        return MetricsPipeline(
            session_factory=factory,  # type: ignore[arg-type]
            geo=_FakeGeo(),
            salt_rotator=SaltRotator(),
            enabled=enabled,
            counting_enabled=counting_enabled,
        )

    yield _build


# --- subscription wiring ---------------------------------------------------


@pytest.mark.unit
async def test_start_subscribes_to_request_observed(bus: EventBus, pipeline_factory: Any) -> None:
    pipeline = pipeline_factory()
    pipeline.start(bus)
    try:
        assert bus.subscriber_count(RequestObserved) == 1
    finally:
        pipeline.stop()


@pytest.mark.unit
async def test_stop_drops_the_subscription(bus: EventBus, pipeline_factory: Any) -> None:
    pipeline = pipeline_factory()
    pipeline.start(bus)
    pipeline.stop()
    assert bus.subscriber_count(RequestObserved) == 0


@pytest.mark.unit
async def test_disabled_pipeline_does_not_subscribe(bus: EventBus, pipeline_factory: Any) -> None:
    pipeline = pipeline_factory(enabled=False)
    pipeline.start(bus)
    assert bus.subscriber_count(RequestObserved) == 0


# --- handler behavior ------------------------------------------------------


@pytest.mark.unit
async def test_published_event_results_in_one_persisted_sample(
    bus: EventBus, factory: _FakeSessionFactory, pipeline_factory: Any
) -> None:
    pipeline = pipeline_factory()
    pipeline.start(bus)
    try:
        await bus.publish(_raw())
    finally:
        pipeline.stop()

    assert len(factory.captured) == 1
    sample = factory.captured[0]
    assert sample.event_type is MetricEventType.VIEW
    assert sample.resource_iri == "https://example.org/r1"


@pytest.mark.unit
async def test_geo_derivation_reaches_the_persisted_sample(
    bus: EventBus, factory: _FakeSessionFactory, pipeline_factory: Any
) -> None:
    pipeline = pipeline_factory()
    pipeline.start(bus)
    try:
        await bus.publish(_raw())
    finally:
        pipeline.stop()

    sample = factory.captured[0]
    assert sample.country_code == "US"
    assert sample.region == "CA"
    assert sample.city == "SF"


@pytest.mark.unit
async def test_visitor_hash_is_present_when_counting_enabled(
    bus: EventBus, factory: _FakeSessionFactory, pipeline_factory: Any
) -> None:
    pipeline = pipeline_factory(counting_enabled=True)
    pipeline.start(bus)
    try:
        await bus.publish(_raw())
    finally:
        pipeline.stop()
    assert factory.captured[0].visitor_hash is not None


@pytest.mark.unit
async def test_visitor_hash_is_absent_when_counting_disabled(
    bus: EventBus, factory: _FakeSessionFactory, pipeline_factory: Any
) -> None:
    pipeline = pipeline_factory(counting_enabled=False)
    pipeline.start(bus)
    try:
        await bus.publish(_raw())
    finally:
        pipeline.stop()
    assert factory.captured[0].visitor_hash is None


# --- structural privacy guarantee ------------------------------------------


@pytest.mark.unit
async def test_subject_never_reaches_the_persisted_sample(
    bus: EventBus, factory: _FakeSessionFactory, pipeline_factory: Any
) -> None:
    """Subject is dropped at the anonymizer; pipeline must not undo that."""
    pipeline = pipeline_factory()
    pipeline.start(bus)
    try:
        await bus.publish(_raw(subject="https://idp/alice-secret-handle"))
    finally:
        pipeline.stop()

    sample = factory.captured[0]
    assert "alice-secret-handle" not in repr(sample)
    # The MetricSample dataclass has no `subject` attribute by construction.
    assert not hasattr(sample, "subject")


@pytest.mark.unit
async def test_ip_and_ua_never_reach_the_persisted_sample(
    bus: EventBus, factory: _FakeSessionFactory, pipeline_factory: Any
) -> None:
    pipeline = pipeline_factory()
    pipeline.start(bus)
    try:
        await bus.publish(_raw(ip="203.0.113.99", ua="DistinctiveUA/9"))
    finally:
        pipeline.stop()

    sample = factory.captured[0]
    assert "203.0.113.99" not in repr(sample)
    assert "DistinctiveUA" not in repr(sample)


# --- failure handling ------------------------------------------------------


class _ExplodingSessionFactory:
    """Raises when used as a context manager, to mimic a DB outage."""

    def __call__(self) -> Any:
        return self

    async def __aenter__(self) -> Any:
        raise RuntimeError("simulated DB outage")

    async def __aexit__(self, *args: Any) -> None:
        return None


@pytest.mark.unit
async def test_handler_swallows_persistence_errors(bus: EventBus) -> None:
    """A handler crash must not bubble up via the bus."""
    pipeline = MetricsPipeline(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        geo=_FakeGeo(),
        salt_rotator=SaltRotator(),
        enabled=True,
        counting_enabled=True,
    )
    pipeline.start(bus)
    try:
        # publish() must complete without raising, even though the
        # handler hits a runtime error.
        await bus.publish(_raw())
    finally:
        pipeline.stop()
