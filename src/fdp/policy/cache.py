"""Materialized authorization index — schema and async repository.

The ORM model :class:`AuthzIndex` mirrors architecture §9.4:
``(subject_key, action, graph_uri, decision, policy_version)``. The
:class:`CacheRepository` exposes the small set of operations the PDP and
the metadata module need: upsert a decision, look one up, list all
graphs a subject can act on, and invalidate by resource or by subject.

``subject_key`` is the user URI plus a short hash of the *current* role
set for authenticated users, and a fixed sentinel for anonymous. Hashing
the roles into the key makes role-change invalidation a delete-by-subject
operation rather than a full recompute.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from fdp.storage.postgres.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from fdp.shared.context import RequestContext


ANONYMOUS_SUBJECT_KEY = "anonymous"


class AuthzIndex(Base):
    """One row per cached ``(subject_key, action, graph_uri)`` decision."""

    __tablename__ = "authz_index"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_key: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    graph_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint(
            "subject_key", "action", "graph_uri", name="uq_authz_subject_action_graph"
        ),
        Index("ix_authz_graph_uri", "graph_uri"),
        Index("ix_authz_subject_key", "subject_key"),
    )


def compute_subject_key(ctx: RequestContext) -> str:
    """Derive the cache key for the subject in ``ctx``.

    Anonymous → :data:`ANONYMOUS_SUBJECT_KEY`. Authenticated → the
    subject URI joined to a short hash of the sorted role set, so a role
    change produces a different key (and therefore a cache miss).
    """
    if ctx.is_anonymous:
        return ANONYMOUS_SUBJECT_KEY
    role_bytes = "|".join(sorted(ctx.roles)).encode("utf-8")
    digest = hashlib.sha256(role_bytes).hexdigest()[:16]
    return f"{ctx.subject}#{digest}"


class CacheRepository:
    """Async repository over :class:`AuthzIndex`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        subject_key: str,
        action: str,
        graph_uri: str,
        decision: str,
        policy_version: str | None,
    ) -> None:
        """Insert or update the decision for the composite key."""
        now = datetime.now(UTC)
        stmt = pg_insert(AuthzIndex).values(
            subject_key=subject_key,
            action=action,
            graph_uri=graph_uri,
            decision=decision,
            policy_version=policy_version,
            computed_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_authz_subject_action_graph",
            set_={
                "decision": stmt.excluded.decision,
                "policy_version": stmt.excluded.policy_version,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        await self._session.execute(stmt)

    async def lookup(
        self,
        *,
        subject_key: str,
        action: str,
        graph_uri: str,
    ) -> AuthzIndex | None:
        stmt = select(AuthzIndex).where(
            AuthzIndex.subject_key == subject_key,
            AuthzIndex.action == action,
            AuthzIndex.graph_uri == graph_uri,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def authorized_resources(
        self,
        *,
        subject_key: str,
        action: str,
    ) -> set[str]:
        """Return every ``graph_uri`` cached as ``permit`` for the subject."""
        stmt = select(AuthzIndex.graph_uri).where(
            AuthzIndex.subject_key == subject_key,
            AuthzIndex.action == action,
            AuthzIndex.decision == "permit",
        )
        result = await self._session.execute(stmt)
        return set(result.scalars().all())

    async def invalidate_by_resource(self, graph_uri: str) -> int:
        """Drop every cached decision for ``graph_uri``. Returns rows deleted."""
        stmt = delete(AuthzIndex).where(AuthzIndex.graph_uri == graph_uri)
        return await self._execute_delete(stmt)

    async def invalidate_by_subject(self, subject_key: str) -> int:
        """Drop every cached decision for ``subject_key``. Returns rows deleted."""
        stmt = delete(AuthzIndex).where(AuthzIndex.subject_key == subject_key)
        return await self._execute_delete(stmt)

    async def invalidate_many_resources(self, graph_uris: Iterable[str]) -> int:
        uris = list(graph_uris)
        if not uris:
            return 0
        stmt = delete(AuthzIndex).where(AuthzIndex.graph_uri.in_(uris))
        return await self._execute_delete(stmt)

    async def _execute_delete(self, stmt: Any) -> int:
        """Execute a DELETE and return the affected row count.

        ``Result.rowcount`` is documented for cursor-style results but not
        present on the generic ``Result`` type stubs; the cast keeps the
        call concise and pyright-clean.
        """
        result = await self._session.execute(stmt)
        return cast("int | None", getattr(result, "rowcount", None)) or 0


__all__ = [
    "ANONYMOUS_SUBJECT_KEY",
    "AuthzIndex",
    "CacheRepository",
    "compute_subject_key",
]
