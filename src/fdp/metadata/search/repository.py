"""Postgres full-text search repository (Phase 7).

Owns the ``tsvector`` write path and the ranked, visibility-gated query. All
SQL is built through SQLAlchemy expressions with bound parameters — no string
interpolation (CLAUDE.md). This module is Postgres-specific; the FTS query is
exercised by integration tests, not the SQLite unit suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Float, and_, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fdp.metadata.search.model import SearchRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.elements import ColumnElement

    from fdp.metadata.search.extract import ExtractedRecord


PUBLISHED = "PUBLISHED"


@dataclass(frozen=True)
class SearchQuery:
    """Resolved query the repository executes (visibility already decided)."""

    text: str | None
    types: tuple[str, ...] = ()
    license: str | None = None
    updated_from: datetime | None = None
    updated_to: datetime | None = None
    language: str = "english"
    offset: int = 0
    limit: int = 20
    # Visibility (ADR-0010): anonymous sees only the public set; an
    # authenticated caller additionally sees the graphs in ``visible``.
    anonymous: bool = True
    visible: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchHit:
    record_iri: str
    type_iri: str | None
    title: str | None
    description: str | None
    license: str | None
    state: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class FacetBucket:
    value: str
    count: int


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    total: int = 0
    facet_type: list[FacetBucket] = field(default_factory=list)
    facet_license: list[FacetBucket] = field(default_factory=list)


class SearchIndexRepository:
    """Async upsert/delete + ranked query over ``metadata_search``."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- write -------------------------------------------------------------

    async def upsert(self, rec: ExtractedRecord, *, anon_read: bool, language: str) -> None:
        """Insert/replace the row for ``rec``, recomputing its ``tsvector``."""
        state = rec.state.value if rec.state is not None else None
        search_text = func.to_tsvector(language, rec.search_source)
        values: dict[str, Any] = {
            "record_iri": rec.record_iri,
            "type_iri": rec.type_iri,
            "title": rec.title,
            "description": rec.description,
            "license": rec.license,
            "keywords": rec.keywords,
            "search_text": search_text,
            "state": state,
            "anon_read": anon_read,
            "updated_at": rec.updated_at,
            "language": language,
        }
        stmt = pg_insert(SearchRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SearchRow.record_iri],
            set_={k: v for k, v in values.items() if k != "record_iri"},
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def delete(self, record_iri: str) -> None:
        async with self._session_factory() as session:
            await session.execute(delete(SearchRow).where(SearchRow.record_iri == record_iri))
            await session.commit()

    async def clear_all(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(delete(SearchRow))
            await session.commit()
        return cast("int | None", getattr(result, "rowcount", None)) or 0

    # --- read --------------------------------------------------------------

    async def search(self, q: SearchQuery) -> SearchResult:
        async with self._session_factory() as session:
            hits, total = await self._run_query(session, q)
            facet_type = await self._facet(session, q, SearchRow.type_iri, exclude="type")
            facet_license = await self._facet(session, q, SearchRow.license, exclude="license")
        return SearchResult(
            hits=hits, total=total, facet_type=facet_type, facet_license=facet_license
        )

    async def _run_query(
        self, session: AsyncSession, q: SearchQuery
    ) -> tuple[list[SearchHit], int]:
        where = self._where(q)
        rank = self._rank(q)
        stmt = (
            select(
                SearchRow.record_iri,
                SearchRow.type_iri,
                SearchRow.title,
                SearchRow.description,
                SearchRow.license,
                SearchRow.state,
                SearchRow.updated_at,
            )
            .where(*where)
            .order_by(rank.desc(), SearchRow.updated_at.desc().nullslast())
            .offset(q.offset)
            .limit(q.limit)
        )
        rows = (await session.execute(stmt)).all()
        hits = [
            SearchHit(
                record_iri=r[0],
                type_iri=r[1],
                title=r[2],
                description=r[3],
                license=r[4],
                state=r[5],
                updated_at=r[6],
            )
            for r in rows
        ]
        count_stmt = select(func.count()).select_from(SearchRow).where(*where)
        total = int((await session.execute(count_stmt)).scalar_one())
        return hits, total

    async def _facet(
        self,
        session: AsyncSession,
        q: SearchQuery,
        column: Any,
        *,
        exclude: str,
    ) -> list[FacetBucket]:
        where = self._where(q, exclude=exclude)
        stmt = (
            select(column, func.count().label("n"))
            .where(*where, column.is_not(None))
            .group_by(column)
            .order_by(func.count().desc(), column)
        )
        rows = (await session.execute(stmt)).all()
        return [FacetBucket(value=str(r[0]), count=int(r[1])) for r in rows]

    # --- predicates --------------------------------------------------------

    def _where(self, q: SearchQuery, *, exclude: str | None = None) -> list[Any]:
        conds: list[Any] = [self._visibility(q)]
        text_cond = self._text_condition(q)
        if text_cond is not None:
            conds.append(text_cond)
        if exclude != "type" and q.types:
            conds.append(SearchRow.type_iri.in_(q.types))
        if exclude != "license" and q.license is not None:
            conds.append(SearchRow.license == q.license)
        if q.updated_from is not None:
            conds.append(SearchRow.updated_at >= q.updated_from)
        if q.updated_to is not None:
            conds.append(SearchRow.updated_at <= q.updated_to)
        return conds

    @staticmethod
    def _visibility(q: SearchQuery) -> ColumnElement[bool]:
        public = and_(SearchRow.state == PUBLISHED, SearchRow.anon_read.is_(True))
        if q.anonymous or not q.visible:
            return public
        return or_(public, SearchRow.record_iri.in_(q.visible))

    @staticmethod
    def _text_condition(q: SearchQuery) -> ColumnElement[bool] | None:
        if not q.text or not q.text.strip():
            return None
        tsquery = func.plainto_tsquery(q.language, q.text)
        return SearchRow.search_text.op("@@")(tsquery)

    @staticmethod
    def _rank(q: SearchQuery) -> ColumnElement[float]:
        if not q.text or not q.text.strip():
            # No text query → rank is constant; order falls back to updated_at.
            return func.cast(literal(0), Float())
        tsquery = func.plainto_tsquery(q.language, q.text)
        return func.ts_rank(SearchRow.search_text, tsquery)


__all__ = [
    "FacetBucket",
    "SearchHit",
    "SearchIndexRepository",
    "SearchQuery",
    "SearchResult",
]
