"""Tests for ``fdp.shared.events``."""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass

import pytest

from fdp.shared.events import Event, EventBus


@dataclass(frozen=True)
class RecordViewed(Event):
    record_id: str


@dataclass(frozen=True)
class RecordDeleted(Event):
    record_id: str


class Recorder:
    """Bound-method subscriber used to exercise WeakMethod handling."""

    def __init__(self) -> None:
        self.seen: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.seen.append(event)


@pytest.mark.unit
async def test_bound_method_handler_receives_event() -> None:
    bus = EventBus()
    recorder = Recorder()
    sub = bus.subscribe(RecordViewed, recorder.handle)

    await bus.publish(RecordViewed(record_id="r-1"))

    assert recorder.seen == [RecordViewed(record_id="r-1")]
    sub.unsubscribe()


@pytest.mark.unit
async def test_handler_exception_does_not_break_other_handlers() -> None:
    bus = EventBus()
    recorder = Recorder()

    async def angry(_event: Event) -> None:
        raise RuntimeError("boom")

    sub_a = bus.subscribe(RecordViewed, angry)
    sub_b = bus.subscribe(RecordViewed, recorder.handle)

    await bus.publish(RecordViewed(record_id="r-2"))

    assert recorder.seen == [RecordViewed(record_id="r-2")]
    # The angry handler ran (and was logged), the second handler also ran.
    sub_a.unsubscribe()
    sub_b.unsubscribe()


@pytest.mark.unit
async def test_handler_for_other_event_type_is_not_called() -> None:
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(RecordViewed, recorder.handle)

    await bus.publish(RecordDeleted(record_id="r-3"))

    assert recorder.seen == []


@pytest.mark.unit
async def test_dropping_subscription_garbage_collects_local_handler() -> None:
    bus = EventBus()

    def install() -> None:
        async def local_handler(_event: Event) -> None:
            pytest.fail("dead handler should not run")

        bus.subscribe(RecordViewed, local_handler)
        # neither `local_handler` nor the Subscription is retained on return

    install()
    gc.collect()

    # publish should prune the dead ref and call nothing
    await bus.publish(RecordViewed(record_id="r-4"))
    assert bus.subscriber_count(RecordViewed) == 0


@pytest.mark.unit
async def test_concurrent_publishes_dispatch_all_handlers() -> None:
    bus = EventBus()
    recorder = Recorder()
    sub = bus.subscribe(RecordViewed, recorder.handle)

    await asyncio.gather(
        bus.publish(RecordViewed(record_id="a")),
        bus.publish(RecordViewed(record_id="b")),
    )

    seen_ids = sorted(event.record_id for event in recorder.seen if isinstance(event, RecordViewed))
    assert seen_ids == ["a", "b"]
    sub.unsubscribe()


@pytest.mark.unit
async def test_explicit_unsubscribe_stops_future_dispatches() -> None:
    bus = EventBus()
    recorder = Recorder()
    sub = bus.subscribe(RecordViewed, recorder.handle)
    sub.unsubscribe()
    gc.collect()

    await bus.publish(RecordViewed(record_id="x"))
    assert recorder.seen == []
