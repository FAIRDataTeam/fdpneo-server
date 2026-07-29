"""``POST /search`` router (Phase 7.2).

Public endpoint (the discovery page is used pre-login); the *results* are gated
per caller by the service. Anonymous callers see only the public set;
authenticated callers also see what they may read per ODRL + state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends

from fdpneo_server.identity.deps import current_context
from fdpneo_server.metadata.search.service import SearchRequest, SearchResponse
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import NotFound

if TYPE_CHECKING:
    from fdpneo_server.metadata.search.service import SearchService


def build_search_router(*, service: SearchService) -> APIRouter:
    """Construct the search router. 404s when search is disabled."""
    router = APIRouter(tags=["search"])

    @router.post("/search", response_model=SearchResponse, name="search")
    async def search(  # pyright: ignore[reportUnusedFunction]
        body: SearchRequest,
        ctx: Annotated[RequestContext, Depends(current_context)],
    ) -> SearchResponse:
        if not service.enabled:
            raise NotFound("search is not enabled on this deployment")
        return await service.search(ctx, body)

    return router


__all__ = ["build_search_router"]
