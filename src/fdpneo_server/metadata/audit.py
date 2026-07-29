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
* The subscriber mirrors :class:`fdpneo_server.metrics.pipeline.MetricsPipeline`
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

from fdpneo_server.metadata.events import (
    RecordCreated,
    RecordDeleted,
    RecordModified,
    RecordStateChanged,
)
from fdpneo_server.shared.events import AdminActionAudited
from fdpneo_server.storage.postgres.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdpneo_server.shared.events import EventBus, Subscription

log = structlog.get_logger(__name__)


class AuditOperation(StrEnum):
    """Stable string identifiers persisted in ``record_audit.operation``."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    STATE_CHANGE = "state_change"
    # User-management actions via the /users facade (audit R-11).
    USER_CREATE = "user_create"
    USER_UPDATE = "user_update"
    USER_DELETE = "user_delete"


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
        """Register handlers for record-modification + admin-action events."""
        self._subs.extend(
            [
                bus.subscribe(RecordCreated, self._on_created),
                bus.subscribe(RecordModified, self._on_modified),
                bus.subscribe(RecordDeleted, self._on_deleted),
                bus.subscribe(RecordStateChanged, self._on_state_changed),
                bus.subscribe(AdminActionAudited, self._on_admin_action),
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
            operation=AuditOperation.CREATE.value,
            subject=event.subject,
            etag=event.etag,
            occurred_at=event.timestamp,
        )

    async def _on_modified(self, event: RecordModified) -> None:
        await self._persist(
            record_iri=event.record_iri,
            operation=AuditOperation.MODIFY.value,
            subject=event.subject,
            etag=event.etag,
            occurred_at=event.timestamp,
        )

    async def _on_deleted(self, event: RecordDeleted) -> None:
        await self._persist(
            record_iri=event.record_iri,
            operation=AuditOperation.DELETE.value,
            subject=event.subject,
            etag=None,  # no post-delete content to fingerprint
            occurred_at=event.timestamp,
        )

    async def _on_state_changed(self, event: RecordStateChanged) -> None:
        await self._persist(
            record_iri=event.record_iri,
            operation=AuditOperation.STATE_CHANGE.value,
            subject=event.subject,
            # No content fingerprint for a lifecycle change; the etag column
            # carries the transition for at-a-glance audit reads.
            etag=f"{event.from_state}->{event.to_state}",
            occurred_at=event.timestamp,
        )

    async def _on_admin_action(self, event: AdminActionAudited) -> None:
        # Generic admin actions (e.g. /users mutations, R-11). ``target`` is the
        # acted-on resource id; ``operation`` is the stable code the producer set.
        await self._persist(
            record_iri=event.target,
            operation=event.operation,
            subject=event.subject,
            etag=None,
            occurred_at=event.timestamp,
        )

    async def _persist(
        self,
        *,
        record_iri: str,
        operation: str,
        subject: str | None,
        etag: str | None,
        occurred_at: datetime,
    ) -> None:
        try:
            async with self._session_factory() as session:
                row = RecordAuditRow(
                    record_iri=record_iri,
                    operation=operation,
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
                operation=operation,
                error=repr(err),
            )


__all__ = ["AuditLog", "AuditOperation", "RecordAuditRow"]
