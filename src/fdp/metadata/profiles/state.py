"""``profile_applied`` marker — schema and async repository.

A successful :func:`apply_profile` writes one row here. The unique
index from migration 0004 (``ux_profile_applied_singleton``) enforces
"at most one applied profile per deployment", so an attempt to insert
a second row before clearing the first surfaces as a Postgres
integrity error rather than overwriting the prior record silently.

The :class:`ProfileStateRepository` is the only code path allowed to
mutate this row. ``clear()`` is wired into the ``--force`` re-apply
flow and into integration-test teardown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import BigInteger, DateTime, String, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from fdp.storage.postgres.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ProfileAppliedRow(Base):
    """One row recording the currently-applied profile."""

    __tablename__ = "profile_applied"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)


class ProfileStateRepository:
    """Async repository over ``profile_applied``.

    Sessions are passed in; the caller owns the transaction. Methods do
    not commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def current(self) -> ProfileAppliedRow | None:
        """Return the applied profile, or ``None`` if uninitialized."""
        stmt = select(ProfileAppliedRow).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def is_applied(self) -> bool:
        return await self.current() is not None

    async def record(
        self,
        *,
        name: str,
        version: str,
        manifest_checksum: str,
        applied_at: datetime | None = None,
    ) -> ProfileAppliedRow:
        """Insert the singleton row.

        Raises ``IntegrityError`` if a row already exists — callers that
        want force-re-apply semantics must call :meth:`clear` first.
        """
        row = ProfileAppliedRow(
            name=name,
            version=version,
            applied_at=applied_at or datetime.now(UTC),
            manifest_checksum=manifest_checksum,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def clear(self) -> int:
        """Drop the applied-profile row. Returns rows deleted (0 or 1)."""
        stmt = delete(ProfileAppliedRow)
        result = await self._session.execute(stmt)
        return cast("int | None", getattr(result, "rowcount", None)) or 0

    @staticmethod
    def to_dict(row: ProfileAppliedRow) -> dict[str, Any]:
        """Render ``row`` as a JSON-friendly dict for CLI output."""
        return {
            "name": row.name,
            "version": row.version,
            "applied_at": row.applied_at.isoformat(),
            "manifest_checksum": row.manifest_checksum,
        }


__all__ = ["ProfileAppliedRow", "ProfileStateRepository"]
