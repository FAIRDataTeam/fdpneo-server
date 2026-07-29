"""Per-subject principal record — the FDP's view of a subject's IdP roles.

One row per OIDC subject holding the roles/groups the IdP **most recently
asserted** for it. The auth middleware upserts this on every successful JWT
login (throttled); API-key authentication reads it so a long-lived key
reflects the owner's *current* authorization rather than a frozen snapshot
(ADR-0011 §4).

This is the seam the deferred IdP role-to-FDP-role sync (architecture §15)
will eventually populate without a token; today it is fed opportunistically
from interactive logins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from fdpneo_server.storage.postgres.models import Base
from fdpneo_server.storage.postgres.types import AwareDateTime

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class SubjectPrincipalRow(Base):
    """Latest IdP-asserted roles/groups for one subject."""

    __tablename__ = "subject_principal"

    subject: Mapped[str] = mapped_column(String(2048), primary_key=True)
    roles_json: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    groups_json: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)


@dataclass(frozen=True)
class Principal:
    """A subject's resolved roles/groups."""

    subject: str
    roles: frozenset[str]
    groups: frozenset[str]


class SubjectPrincipalRepository:
    """Async upsert/read over ``subject_principal``."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, subject: str) -> Principal | None:
        async with self._session_factory() as session:
            row = await session.get(SubjectPrincipalRow, subject)
            if row is None:
                return None
            return _to_principal(row)

    async def record(self, subject: str, *, roles: frozenset[str], groups: frozenset[str]) -> None:
        """Upsert the subject's current roles/groups.

        Idempotent and best-effort: callers on the auth hot path swallow
        failures so a transient DB hiccup never rejects an otherwise-valid
        login.
        """
        payload_roles = sorted(roles)
        payload_groups = sorted(groups)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            stmt = pg_insert(SubjectPrincipalRow).values(
                subject=subject,
                roles_json=payload_roles,
                groups_json=payload_groups,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SubjectPrincipalRow.subject],
                set_={
                    "roles_json": stmt.excluded.roles_json,
                    "groups_json": stmt.excluded.groups_json,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
            await session.commit()


def _to_principal(row: SubjectPrincipalRow) -> Principal:
    return Principal(
        subject=row.subject,
        roles=frozenset(_as_str_list(row.roles_json)),
        groups=frozenset(_as_str_list(row.groups_json)),
    )


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)]


__all__ = [
    "Principal",
    "SubjectPrincipalRepository",
    "SubjectPrincipalRow",
]
