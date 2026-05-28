"""Record-modification audit: ORM model + event-bus subscriber.

The LDP router publishes ``RecordCreated`` / ``RecordModified`` /
``RecordDeleted`` events on every successful write. The audit
subscriber here is the second consumer of those events (alongside the
metrics pipeline) and persists one row per event into Postgres.

Architectural intent:

* The triple store's per-record ``<record>/audit`` graph (architecture
  §6) materialises *ODRL Agreements* when policy grants fire. This
  module owns a different audit trail: who modified the LDP
  representation, when, with what fingerprint (post-write ETag).
* Failures during persistence are logged and swallowed — losing one
  audit row must never reject a successful write. Operators
  monitoring the ``audit_log_insert_failed`` log event spot deeper
  Postgres trouble.
* The subscriber mirrors :class:`fdp.metrics.pipeline.MetricsPipeline`
  shape: a singleton with ``start(bus)`` / ``stop()`` invoked from the
  FastAPI lifespan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from fdp.metadata.events import RecordCreated, RecordDeleted, RecordModified
from fdp.storage.postgres.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.shared.events import EventBus, Subscription

log = structlog.get_logger(__name__)


class AuditOperation(StrEnum):
    """Stable string identifiers persisted in ``record_audit.operation``."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class RecordAuditRow(Base):
    """One row per record-modification event."""

    __tablename__ = "record_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    record_iri: Mapped[str] = mapped_column(String(2048), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_record_audit_record_iri", "record_iri"),
        Index("ix_record_audit_occurred_at", "occurred_at"),
        Index("ix_record_audit_subject", "subject"),
    )


class AuditLog:
    """Subscribes to record-modification events and writes audit rows.

    One instance per process. :meth:`start` registers handlers on the
    bus and returns; :meth:`stop` drops the subscriptions. The handler
    awaits its own Postgres insert (no fire-and-forget): an in-process
    INSERT is cheap and synchronous-feeling persistence keeps the
    audit row's ordering meaningful relative to other consumers'.
    """

    __slots__ = ("_session_factory", "_subs")

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._subs: list[Subscription] = []

    def start(self, bus: EventBus) -> None:
        """Register handlers for the three record-modification event types."""
        self._subs.extend(
            [
                bus.subscribe(RecordCreated, self._on_created),
                bus.subscribe(RecordModified, self._on_modified),
                bus.subscribe(RecordDeleted, self._on_deleted),
            ]
        )
        log.info("audit_log_started")

    def stop(self) -> None:
        """Drop every bus subscription. Idempotent."""
        for sub in self._subs:
            sub.unsubscribe()
        self._subs.clear()

    # --- handlers --------------------------------------------------------

    async def _on_created(self, event: RecordCreated) -> None:
        await self._persist(
            record_iri=event.record_iri,
            operation=AuditOperation.CREATE,
            subject=event.subject,
            etag=event.etag,
            occurred_at=event.timestamp,
        )

    async def _on_modified(self, event: RecordModified) -> None:
        await self._persist(
            record_iri=event.record_iri,
            operation=AuditOperation.MODIFY,
            subject=event.subject,
            etag=event.etag,
            occurred_at=event.timestamp,
        )

    async def _on_deleted(self, event: RecordDeleted) -> None:
        await self._persist(
            record_iri=event.record_iri,
            operation=AuditOperation.DELETE,
            subject=event.subject,
            etag=None,  # no post-delete content to fingerprint
            occurred_at=event.timestamp,
        )

    async def _persist(
        self,
        *,
        record_iri: str,
        operation: AuditOperation,
        subject: str | None,
        etag: str | None,
        occurred_at: datetime,
    ) -> None:
        try:
            async with self._session_factory() as session:
                row = RecordAuditRow(
                    record_iri=record_iri,
                    operation=operation.value,
                    subject=subject,
                    etag=etag,
                    occurred_at=occurred_at,
                )
                session.add(row)
                await session.commit()
        except Exception as err:
            log.warning(
                "audit_log_insert_failed",
                record_iri=record_iri,
                operation=operation.value,
                error=repr(err),
            )


__all__ = ["AuditLog", "AuditOperation", "RecordAuditRow"]
