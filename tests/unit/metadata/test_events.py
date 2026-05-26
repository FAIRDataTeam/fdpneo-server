"""Unit tests for :mod:`fdp.metadata.events`."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from fdp.metadata.events import RecordModified
from fdp.shared.events import EventBus

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_record_modified_is_frozen_with_expected_fields() -> None:
    evt = RecordModified(
        record_iri="https://example.org/r1",
        subject="https://idp/alice",
        etag="abc",
        timestamp=NOW,
    )
    assert evt.record_iri == "https://example.org/r1"
    assert evt.subject == "https://idp/alice"
    assert evt.etag == "abc"
    assert evt.timestamp == NOW
    with pytest.raises(dataclasses.FrozenInstanceError):
        evt.record_iri = "https://example.org/other"  # type: ignore[misc]


@pytest.mark.unit
async def test_record_modified_round_trips_through_event_bus() -> None:
    bus = EventBus()
    received: list[RecordModified] = []

    async def handler(evt: RecordModified) -> None:
        received.append(evt)

    sub = bus.subscribe(RecordModified, handler)
    try:
        await bus.publish(
            RecordModified(
                record_iri="https://example.org/r1",
                subject=None,
                etag="deadbeef",
                timestamp=NOW,
            )
        )
    finally:
        sub.unsubscribe()

    assert len(received) == 1
    assert received[0].record_iri == "https://example.org/r1"
    assert received[0].subject is None
    assert received[0].etag == "deadbeef"
