"""Unit tests for :mod:`fdpneo_server.metadata.events`."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from fdpneo_server.metadata.events import RecordCreated, RecordDeleted, RecordModified
from fdpneo_server.shared.events import EventBus

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


@pytest.mark.unit
def test_record_created_is_frozen_with_expected_fields() -> None:
    evt = RecordCreated(
        record_iri="https://example.org/r1",
        subject="https://idp/alice",
        etag="abc",
        timestamp=NOW,
    )
    assert evt.record_iri == "https://example.org/r1"
    assert evt.etag == "abc"
    with pytest.raises(dataclasses.FrozenInstanceError):
        evt.record_iri = "x"  # type: ignore[misc]


@pytest.mark.unit
def test_record_deleted_omits_etag() -> None:
    evt = RecordDeleted(
        record_iri="https://example.org/r1",
        subject="https://idp/alice",
        timestamp=NOW,
    )
    assert evt.record_iri == "https://example.org/r1"
    # No etag attribute by design.
    assert not hasattr(evt, "etag")


@pytest.mark.unit
async def test_record_created_and_deleted_round_trip_through_bus() -> None:
    bus = EventBus()
    created: list[RecordCreated] = []
    deleted: list[RecordDeleted] = []

    async def on_created(evt: RecordCreated) -> None:
        created.append(evt)

    async def on_deleted(evt: RecordDeleted) -> None:
        deleted.append(evt)

    s1 = bus.subscribe(RecordCreated, on_created)
    s2 = bus.subscribe(RecordDeleted, on_deleted)
    try:
        await bus.publish(
            RecordCreated(
                record_iri="https://example.org/r1",
                subject="https://idp/alice",
                etag="e1",
                timestamp=NOW,
            )
        )
        await bus.publish(
            RecordDeleted(
                record_iri="https://example.org/r1",
                subject="https://idp/alice",
                timestamp=NOW,
            )
        )
    finally:
        s1.unsubscribe()
        s2.unsubscribe()

    assert len(created) == 1
    assert len(deleted) == 1
