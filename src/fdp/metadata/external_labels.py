"""External (remote) label resolution — the deferred third source of ``/labels``.

Phase 21 (ADR-0012 §8 / architecture §8.6). The public ``GET /fdp-api/labels``
endpoint resolves an IRI to a human label from the local knowledge graph and a
curated inline map. This module adds a third source: dereferencing an *external*
IRI (a ROR org, a DOI, an ORCID, a SKOS term) over content-negotiated RDF,
extracting a label, and caching it.

Two collaborators, both used by :class:`fdp.metadata.labels.LabelResolver`:

* :class:`ExternalLabelCache` — a Postgres-backed ``(iri, language) -> label``
  cache (``metadata_external_labels``). It stores **negative** results too (a
  ``NULL`` label) so an unresolvable IRI isn't re-fetched until its shorter TTL
  expires. This is the durable layer; the resolver keeps its in-memory TTL cache
  as a hot layer in front of it.
* :class:`ExternalLabelFetcher` — the outbound fetch (added in 21.3): allow-list
  gated, SSRF-guarded per redirect hop, size/time-capped, generic RDF parse.

Security posture mirrors remote schema sync: off by default, only hosts on the
configured allow-list are dereferenced, and every fetch is bounded. See
:class:`fdp.config.RemoteLabelSettings`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from fdp.storage.postgres.models import Base
from fdp.storage.postgres.types import AwareDateTime

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


# --- ORM -------------------------------------------------------------------


class ExternalLabelRow(Base):
    """One cached external label, keyed by ``(iri, language)``.

    A ``NULL`` ``label`` is a cached *miss* (the IRI was fetched but no label was
    found, or the fetch failed) — remembered for a shorter TTL than a hit so a
    transient outage self-heals without hammering the remote.
    """

    __tablename__ = "metadata_external_labels"

    iri: Mapped[str] = mapped_column(String(2048), primary_key=True)
    language: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    source_host: Mapped[str | None] = mapped_column(String(255), nullable=True)


# --- cache repository ------------------------------------------------------


class ExternalLabelCache:
    """Async persistent cache over ``metadata_external_labels``.

    Cross-dialect: reads/writes go through the ORM (``merge`` for upsert) so the
    unit suite exercises it on SQLite while production runs on Postgres.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_many(
        self, iris: Sequence[str], *, language: str
    ) -> dict[str, str | None]:
        """Return fresh cached entries for ``iris`` in ``language``.

        The result maps each IRI that has a **non-expired** row to its label
        (``None`` for a cached miss). IRIs absent from the map are simply not
        cached (or expired) and should be resolved by the caller. Callers must
        distinguish "key present with value ``None``" (cached negative) from
        "key absent" (unknown).
        """
        if not iris:
            return {}
        now = datetime.now(UTC)
        stmt = select(ExternalLabelRow.iri, ExternalLabelRow.label).where(
            ExternalLabelRow.iri.in_(list(iris)),
            ExternalLabelRow.language == language,
            ExternalLabelRow.expires_at > now,
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {row.iri: row.label for row in rows}

    async def upsert(
        self,
        iri: str,
        language: str,
        label: str | None,
        *,
        ttl_seconds: int,
        source_host: str | None = None,
    ) -> None:
        """Insert or replace the cache entry for ``(iri, language)``."""
        now = datetime.now(UTC)
        row = ExternalLabelRow(
            iri=iri,
            language=language,
            label=label,
            resolved_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            source_host=source_host,
        )
        async with self._session_factory() as session:
            await session.merge(row)
            await session.commit()

    async def purge_expired(self) -> int:
        """Delete every expired row. Returns the number removed (best-effort)."""
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(ExternalLabelRow).where(ExternalLabelRow.expires_at <= now)
            )
            await session.commit()
        return cast("int | None", getattr(result, "rowcount", None)) or 0


__all__ = ["ExternalLabelCache", "ExternalLabelRow"]
