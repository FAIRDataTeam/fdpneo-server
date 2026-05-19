"""In-process asynchronous event bus.

**Responsibilities**

* Let bounded contexts publish domain events (record viewed, metadata
  modified, login completed) without coupling to their consumers.
* Let consumers — metrics, audit log — subscribe to event types without
  reaching back into the producer.
* Hold subscribers via weak references so a torn-down module does not keep
  the bus alive. :meth:`EventBus.subscribe` returns a :class:`Subscription`
  that holds the handler strongly; drop it to unsubscribe.

**Non-responsibilities**

* Anonymization. Metrics anonymizes events at its subscriber boundary
  (ADR-0002); the bus itself does not transform payloads.
* Persistence. Events are in-process only.
"""

from __future__ import annotations

import asyncio
import weakref
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Event:
    """Marker base for events dispatched on the bus.

    Concrete events are frozen dataclasses defined by the producing module.
    """


E = TypeVar("E", bound=Event)
Handler = Callable[[E], Awaitable[None]]


class Subscription:
    """Anchor for a registered handler.

    Holds the handler strongly so callers who keep the Subscription keep the
    subscription alive. Drop the Subscription (let it go out of scope) or
    call :meth:`unsubscribe` to deregister.
    """

    __slots__ = ("__weakref__", "active", "handler")

    def __init__(self, handler: Handler[Event]) -> None:
        self.handler: Handler[Event] | None = handler
        self.active: bool = True

    def unsubscribe(self) -> None:
        self.active = False
        self.handler = None


class EventBus:
    """Dispatches events to registered handlers by exact type."""

    def __init__(self) -> None:
        self._subs: dict[type[Event], list[weakref.ReferenceType[Subscription]]] = defaultdict(list)

    def subscribe(self, event_type: type[E], handler: Handler[E]) -> Subscription:
        """Register ``handler`` for events whose ``type()`` equals ``event_type``.

        The returned :class:`Subscription` keeps the handler alive; callers
        who do not retain it lose the subscription on the next garbage pass.
        """
        # Generic-variance trade-off: handlers are stored as Handler[Event]
        # but called with a concrete subtype.
        sub = Subscription(handler)  # type: ignore[arg-type]
        self._subs[event_type].append(weakref.ref(sub))
        return sub

    def subscriber_count(self, event_type: type[Event]) -> int:
        """Return the number of live, active subscribers (test helper)."""
        self._prune(event_type)
        return len(self._subs.get(event_type, []))

    async def publish(self, event: Event) -> None:
        """Dispatch ``event`` to every live handler concurrently."""
        event_type = type(event)
        refs = self._subs.get(event_type)
        if not refs:
            return

        live: list[Subscription] = []
        survivors: list[weakref.ReferenceType[Subscription]] = []
        for ref in refs:
            sub = ref()
            if sub is None or not sub.active or sub.handler is None:
                continue
            live.append(sub)
            survivors.append(ref)
        self._subs[event_type] = survivors

        if not live:
            return

        handlers = [sub.handler for sub in live if sub.handler is not None]
        results = await asyncio.gather(
            *(handler(event) for handler in handlers),
            return_exceptions=True,
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                log.warning(
                    "event_handler_error",
                    event_type=event_type.__name__,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    error=repr(result),
                )

    def _prune(self, event_type: type[Event]) -> None:
        """Drop dead or inactive subscription refs for ``event_type``."""
        refs = self._subs.get(event_type)
        if not refs:
            return
        self._subs[event_type] = [
            ref
            for ref in refs
            if (sub := ref()) is not None and sub.active and sub.handler is not None
        ]


__all__ = ["Event", "EventBus", "Handler", "Subscription"]
