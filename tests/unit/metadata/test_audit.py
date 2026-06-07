"""Unit tests for :class:`AuditLog` (item #6 of the operational-readiness plan).

The subscriber's contract is narrow: one row per record-modification
event, with operation/subject/etag/timestamp matching the event.
Tests use an in-memory session factory so the row shape is verified
without I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from fdp.metadata.audit import AuditLog, AuditOperation, RecordAuditRow
from fdp.metadata.events import RecordCreated, RecordDeleted, RecordModified
from fdp.shared.events import AdminActionAudited, EventBus

NOW = datetime(2026, 5, 28, 12, 34, 56, tzinfo=UTC)


class _FakeSession:
    """Records rows added; mimics commit / rollback."""

    def __init__(self, captured: list[RecordAuditRow]) -> None:
        self._captured = captured
        self.committed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def add(self, obj: Any) -> None:
        assert isinstance(obj, RecordAuditRow)
        self._captured.append(obj)

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.rows: list[RecordAuditRow] = []

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.rows)


class _ExplodingSessionFactory:
    """Mimics a Postgres outage."""

    def __call__(self) -> Any:
        return self

    async def __aenter__(self) -> Any:
        raise RuntimeError("simulated outage")

    async def __aexit__(self, *args: Any) -> None:
        return None


# --- handler coverage ----------------------------------------------------


@pytest.mark.unit
async def test_record_created_event_writes_create_row() -> None:
    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    try:
        await bus.publish(
            RecordCreated(
                record_iri="https://fdp.example/r-1",
                subject="https://idp/alice",
                etag="abc123",
                timestamp=NOW,
            )
        )
    finally:
        audit.stop()

    assert len(factory.rows) == 1
    row = factory.rows[0]
    assert row.record_iri == "https://fdp.example/r-1"
    assert row.operation == AuditOperation.CREATE.value
    assert row.subject == "https://idp/alice"
    assert row.etag == "abc123"
    assert row.occurred_at == NOW


@pytest.mark.unit
async def test_record_modified_event_writes_modify_row() -> None:
    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    try:
        await bus.publish(
            RecordModified(
                record_iri="https://fdp.example/r-1",
                subject=None,  # anonymous (PEP would normally have blocked it)
                etag="def456",
                timestamp=NOW,
            )
        )
    finally:
        audit.stop()

    assert factory.rows[0].operation == AuditOperation.MODIFY.value
    assert factory.rows[0].subject is None
    assert factory.rows[0].etag == "def456"


@pytest.mark.unit
async def test_record_deleted_event_writes_delete_row_without_etag() -> None:
    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    try:
        await bus.publish(
            RecordDeleted(
                record_iri="https://fdp.example/r-1",
                subject="https://idp/admin",
                timestamp=NOW,
            )
        )
    finally:
        audit.stop()

    row = factory.rows[0]
    assert row.operation == AuditOperation.DELETE.value
    assert row.etag is None  # no post-delete content to fingerprint


# --- subscription lifecycle ----------------------------------------------


@pytest.mark.unit
async def test_start_subscribes_to_all_three_event_types() -> None:
    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    try:
        assert bus.subscriber_count(RecordCreated) == 1
        assert bus.subscriber_count(RecordModified) == 1
        assert bus.subscriber_count(RecordDeleted) == 1
    finally:
        audit.stop()


@pytest.mark.unit
async def test_stop_drops_every_subscription() -> None:
    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    audit.stop()
    assert bus.subscriber_count(RecordCreated) == 0
    assert bus.subscriber_count(RecordModified) == 0
    assert bus.subscriber_count(RecordDeleted) == 0


@pytest.mark.unit
async def test_stop_is_idempotent() -> None:
    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    audit.stop()
    audit.stop()  # must not raise


# --- failure handling ----------------------------------------------------


@pytest.mark.unit
async def test_persistence_failure_does_not_propagate() -> None:
    """A handler crash must not bubble up via the bus and fail the request."""
    audit = AuditLog(session_factory=_ExplodingSessionFactory())  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    try:
        await bus.publish(
            RecordCreated(
                record_iri="https://fdp.example/r-1",
                subject=None,
                etag="x",
                timestamp=NOW,
            )
        )
    finally:
        audit.stop()
    # No assertions on captured rows: the point is that publish() returned.


@pytest.mark.unit
async def test_admin_action_event_writes_user_audit_row() -> None:

    factory = _FakeSessionFactory()
    audit = AuditLog(session_factory=factory)  # type: ignore[arg-type]
    bus = EventBus()
    audit.start(bus)
    try:
        await bus.publish(
            AdminActionAudited(
                target="ba0cf67c-7dca-4e51-bdf8-bf467c3bdb6b",
                operation=AuditOperation.USER_DELETE.value,
                subject="https://idp/admin",
                timestamp=NOW,
            )
        )
    finally:
        audit.stop()

    assert len(factory.rows) == 1
    row = factory.rows[0]
    assert row.record_iri == "ba0cf67c-7dca-4e51-bdf8-bf467c3bdb6b"
    assert row.operation == "user_delete"
    assert row.subject == "https://idp/admin"
    assert row.occurred_at == NOW
