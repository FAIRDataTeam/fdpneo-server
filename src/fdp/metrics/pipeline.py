"""Event-bus subscriber for the metrics pipeline.

Subscribes to :class:`fdp.metrics.events.RequestObserved`, runs it
through :func:`fdp.metrics.anonymize.anonymize`, and persists the
resulting :class:`MetricSample` to ``metrics_raw``.

The handler is the structural guarantee from ADR-0002: ``RequestObserved``
is consumed *only* here, and the only thing that leaves this function is
a ``MetricSample``. The downstream rollup never sees the raw envelope.

Failures inside the handler are logged and swallowed. The event bus
records handler errors too (see :mod:`fdp.shared.events`), but losing
metrics for a single request must never bubble up and fail the request
that produced it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from fdp.metrics.anonymize import anonymize
from fdp.metrics.events import RequestObserved
from fdp.metrics.repository import MetricsRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.metrics.geo import GeoLookup
    from fdp.metrics.salt import SaltRotator
    from fdp.shared.events import EventBus, Subscription

log = structlog.get_logger(__name__)


class MetricsPipeline:
    """Subscribes to :class:`RequestObserved` and persists anonymized samples.

    One instance per process. :meth:`start` registers the bus handler
    and returns; :meth:`stop` drops the subscription, allowing the
    handler reference to be collected.

    Construction takes the collaborators by injection so tests can pass
    fakes; the FastAPI lifespan wires the real ones.
    """

    __slots__ = (
        "_bus",
        "_counting_enabled",
        "_enabled",
        "_geo",
        "_salt",
        "_session_factory",
        "_sub",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        geo: GeoLookup,
        salt_rotator: SaltRotator,
        enabled: bool,
        counting_enabled: bool,
    ) -> None:
        self._session_factory = session_factory
        self._geo = geo
        self._salt = salt_rotator
        self._enabled = enabled
        self._counting_enabled = counting_enabled
        self._bus: EventBus | None = None
        self._sub: Subscription | None = None

    def start(self, bus: EventBus) -> None:
        """Register the handler on ``bus`` if metrics are enabled.

        No-op when ``enabled`` is False; the pipeline is effectively
        inert and the metrics module produces no rows.
        """
        if not self._enabled:
            log.info("metrics_pipeline_disabled")
            return
        self._bus = bus
        self._sub = bus.subscribe(RequestObserved, self._handle)
        log.info("metrics_pipeline_started")

    def stop(self) -> None:
        """Drop the bus subscription. Idempotent."""
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None
        self._bus = None

    async def _handle(self, event: RequestObserved) -> None:
        """Anonymize ``event`` and persist the sample.

        Wrapped in a broad except: any failure here is a metrics-side
        bug and must not propagate to the request handler that
        published the event.
        """
        try:
            sample = anonymize(
                event,
                geo=self._geo,
                salt_rotator=self._salt,
                counting_enabled=self._counting_enabled,
            )
            async with self._session_factory() as session:
                repo = MetricsRepository(session)
                await repo.insert_raw(sample)
                await session.commit()
        except Exception as err:
            log.warning(
                "metrics_pipeline_insert_failed",
                error=repr(err),
                event_type=event.event_type.value,
            )


__all__ = ["MetricsPipeline"]
