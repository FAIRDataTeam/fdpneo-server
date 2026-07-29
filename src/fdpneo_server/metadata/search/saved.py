"""Saved search queries (Phase 7.3) — repository, service, ``/me/saved-queries``.

Owner-scoped named searches. An owner manages their own; an admin may flip the
``shared`` flag to publish one to everyone. Stored ``query`` is validated as a
runnable :class:`SearchRequest` so a saved query can always be replayed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.search.model import SavedQueryRow
from fdpneo_server.metadata.search.service import SearchRequest
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import BadRequest, Forbidden, NotFound

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"


# --- repository ------------------------------------------------------------


class SavedQueryRepository:
    """Async CRUD over ``search_saved_queries``."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, row: SavedQueryRow) -> None:
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def get(self, query_id: str) -> SavedQueryRow | None:
        async with self._session_factory() as session:
            return await session.get(SavedQueryRow, query_id)

    async def list_visible(self, subject: str) -> list[SavedQueryRow]:
        """The subject's own queries plus every shared one."""
        async with self._session_factory() as session:
            stmt = (
                select(SavedQueryRow)
                .where(
                    or_(
                        SavedQueryRow.owner_subject == subject,
                        SavedQueryRow.shared.is_(True),
                    )
                )
                .order_by(SavedQueryRow.created_at.desc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def update(
        self,
        query_id: str,
        *,
        name: str | None,
        query_json: dict[str, Any] | None,
        shared: bool | None,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(SavedQueryRow, query_id)
            if row is None:
                return
            if name is not None:
                row.name = name
            if query_json is not None:
                row.query_json = query_json
            if shared is not None:
                row.shared = shared
            await session.commit()

    async def delete(self, query_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(SavedQueryRow, query_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


# --- DTOs ------------------------------------------------------------------


class SavedQueryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    query: dict[str, Any]


class SavedQueryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    query: dict[str, Any] | None = None
    shared: bool | None = None


class SavedQueryView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    query: dict[str, Any]
    shared: bool
    owner_subject: str = Field(serialization_alias="ownerSubject")
    mine: bool
    created_at: datetime = Field(serialization_alias="createdAt")


class SavedQueryList(BaseModel):
    queries: list[SavedQueryView]


# --- service ---------------------------------------------------------------


class SavedQueryService:
    """List / create / update / delete saved queries with owner+admin rules."""

    def __init__(self, *, repository: SavedQueryRepository) -> None:
        self._repo = repository

    async def list_for(self, ctx: RequestContext) -> list[SavedQueryView]:
        if ctx.subject is None:
            return []
        rows = await self._repo.list_visible(ctx.subject)
        return [_view(row, ctx) for row in rows]

    async def create(self, ctx: RequestContext, body: SavedQueryCreate) -> SavedQueryView:
        if ctx.subject is None:
            raise Forbidden("authentication required")
        query = _validate_query(body.query)
        row = SavedQueryRow(
            id=str(uuid.uuid4()),
            owner_subject=ctx.subject,
            name=body.name,
            query_json=query,
            shared=False,
            created_at=datetime.now(UTC),
        )
        await self._repo.add(row)
        log.info("saved_query_created", id=row.id, subject=ctx.subject)
        return _view(row, ctx)

    async def update(
        self, ctx: RequestContext, query_id: str, body: SavedQueryUpdate
    ) -> SavedQueryView:
        row = await self._repo.get(query_id)
        if row is None:
            raise NotFound(f"no saved query: {query_id}")
        is_admin = _ADMIN_ROLE in ctx.roles
        is_owner = row.owner_subject == ctx.subject
        if not is_owner and not is_admin:
            raise Forbidden("not your saved query")
        # The shared flag is admin-only (owners can't self-publish).
        if body.shared is not None and not is_admin:
            raise Forbidden("only an admin may change the shared flag")
        query = _validate_query(body.query) if body.query is not None else None
        await self._repo.update(query_id, name=body.name, query_json=query, shared=body.shared)
        updated = await self._repo.get(query_id)
        assert updated is not None
        return _view(updated, ctx)

    async def delete(self, ctx: RequestContext, query_id: str) -> None:
        row = await self._repo.get(query_id)
        if row is None:
            raise NotFound(f"no saved query: {query_id}")
        if row.owner_subject != ctx.subject and _ADMIN_ROLE not in ctx.roles:
            raise Forbidden("not your saved query")
        await self._repo.delete(query_id)


def _validate_query(query: dict[str, Any]) -> dict[str, Any]:
    try:
        return SearchRequest.model_validate(query).model_dump(mode="json", by_alias=True)
    except ValidationError as err:
        raise BadRequest(
            "query is not a valid search request", details={"errors": err.errors()}
        ) from err


def _view(row: SavedQueryRow, ctx: RequestContext) -> SavedQueryView:
    return SavedQueryView(
        id=row.id,
        name=row.name,
        query=row.query_json,
        shared=row.shared,
        owner_subject=row.owner_subject,
        mine=row.owner_subject == ctx.subject,
        created_at=row.created_at,
    )


# --- router ----------------------------------------------------------------


def build_saved_queries_router(*, service: SavedQueryService) -> APIRouter:
    """Construct the ``/me/saved-queries`` router. All routes require auth."""
    router = APIRouter(tags=["saved-queries"])

    @router.get("/me/saved-queries", response_model=SavedQueryList, name="saved_query_list")
    async def list_queries(  # pyright: ignore[reportUnusedFunction]
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> SavedQueryList:
        return SavedQueryList(queries=await service.list_for(ctx))

    @router.post(
        "/me/saved-queries",
        response_model=SavedQueryView,
        status_code=201,
        name="saved_query_create",
    )
    async def create_query(  # pyright: ignore[reportUnusedFunction]
        body: SavedQueryCreate,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> SavedQueryView:
        return await service.create(ctx, body)

    @router.put(
        "/me/saved-queries/{query_id}", response_model=SavedQueryView, name="saved_query_update"
    )
    async def update_query(  # pyright: ignore[reportUnusedFunction]
        query_id: str,
        body: SavedQueryUpdate,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> SavedQueryView:
        return await service.update(ctx, query_id, body)

    @router.delete("/me/saved-queries/{query_id}", status_code=204, name="saved_query_delete")
    async def delete_query(  # pyright: ignore[reportUnusedFunction]
        query_id: str,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> None:
        await service.delete(ctx, query_id)

    return router


__all__ = [
    "SavedQueryCreate",
    "SavedQueryList",
    "SavedQueryRepository",
    "SavedQueryService",
    "SavedQueryUpdate",
    "SavedQueryView",
    "build_saved_queries_router",
]
