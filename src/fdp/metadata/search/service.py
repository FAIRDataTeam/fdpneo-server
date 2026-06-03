"""Search query orchestration (Phase 7.2).

Turns a request + caller context into a repository query, applying the
visibility gate (ADR-0010): anonymous callers get only the public set (the
repository's ``state='PUBLISHED' AND anon_read`` branch); authenticated callers
additionally see the graphs in :meth:`StateGate.visible_read_graphs` (their
drafts + any private records they can read). Facet dimensions and labels are
driven by the Phase 9.4 ``search.filters`` runtime setting.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from fdp.metadata.search.repository import FacetBucket, SearchHit, SearchQuery

if TYPE_CHECKING:
    from fdp.config import SearchSettings
    from fdp.metadata.lifecycle import StateGate
    from fdp.metadata.search.repository import SearchIndexRepository, SearchResult
    from fdp.metadata.settings import SettingsRepository
    from fdp.shared.context import RequestContext

# Maps a configured filter predicate (Phase 9.4) to a facet dimension the
# index can compute. Other predicates are ignored (no column to group on).
_PREDICATE_DIMENSION: Final[dict[str, str]] = {
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#type": "type",
    "http://purl.org/dc/terms/license": "license",
}
_DEFAULT_LABELS: Final[dict[str, str]] = {"type": "Type", "license": "License"}


# --- request / response models ---------------------------------------------


class SearchRequest(BaseModel):
    """Body for ``POST /search``."""

    model_config = ConfigDict(populate_by_name=True)

    query: str | None = None
    types: list[str] = Field(default_factory=list)
    license: str | None = None
    updated_from: datetime | None = Field(default=None, alias="from")
    updated_to: datetime | None = Field(default=None, alias="to")
    language: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)


class SearchItem(BaseModel):
    record_iri: str = Field(serialization_alias="recordIri")
    type_iri: str | None = Field(default=None, serialization_alias="typeIri")
    title: str | None = None
    description: str | None = None
    license: str | None = None
    state: str | None = None
    updated_at: datetime | None = Field(default=None, serialization_alias="updatedAt")


class FacetValue(BaseModel):
    value: str
    count: int


class FacetDimension(BaseModel):
    label: str
    values: list[FacetValue]


class SearchResponse(BaseModel):
    items: list[SearchItem]
    total: int
    facets: dict[str, FacetDimension]


# --- service ---------------------------------------------------------------


class SearchService:
    """Composes gating + facet config around the FTS repository."""

    def __init__(
        self,
        *,
        repository: SearchIndexRepository,
        state_gate: StateGate,
        settings_repository: SettingsRepository,
        settings: SearchSettings,
    ) -> None:
        self._repo = repository
        self._gate = state_gate
        self._settings_repo = settings_repository
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    async def search(self, ctx: RequestContext, request: SearchRequest) -> SearchResponse:
        anonymous = ctx.is_anonymous
        visible: tuple[str, ...] = ()
        if not anonymous:
            visible = tuple(sorted(await self._gate.visible_read_graphs(ctx)))
        limit = min(request.limit or self._settings.default_limit, self._settings.max_limit)
        query = SearchQuery(
            text=request.query,
            types=tuple(request.types),
            license=request.license,
            updated_from=request.updated_from,
            updated_to=request.updated_to,
            language=request.language or self._settings.default_language,
            offset=request.offset,
            limit=limit,
            anonymous=anonymous,
            visible=visible,
        )
        result = await self._repo.search(query)
        return SearchResponse(
            items=[_item(h) for h in result.hits],
            total=result.total,
            facets=await self._facets(result),
        )

    async def _facets(self, result: SearchResult) -> dict[str, FacetDimension]:
        labels = await self._facet_labels()
        out: dict[str, FacetDimension] = {}
        if "type" in labels:
            out["type"] = FacetDimension(
                label=labels["type"], values=_facet_values(result.facet_type)
            )
        if "license" in labels:
            out["license"] = FacetDimension(
                label=labels["license"], values=_facet_values(result.facet_license)
            )
        return out

    async def _facet_labels(self) -> dict[str, str]:
        """Which facet dimensions to expose + their labels (Phase 9.4 settings).

        If the deployment has configured ``search.filters``, only the
        dimensions those filters map to are exposed, with their labels;
        otherwise both built-ins (type, license) are returned with defaults.
        """
        try:
            configured = await self._settings_repo.read_with_default("search.filters")
        except Exception:
            configured = None
        filters = getattr(configured, "filters", None) or []
        labels: dict[str, str] = {}
        for flt in filters:
            dimension = _PREDICATE_DIMENSION.get(getattr(flt, "predicate", ""))
            if dimension is not None:
                labels[dimension] = getattr(flt, "label", None) or _DEFAULT_LABELS[dimension]
        return labels or dict(_DEFAULT_LABELS)


def _item(hit: SearchHit) -> SearchItem:
    return SearchItem(
        record_iri=hit.record_iri,
        type_iri=hit.type_iri,
        title=hit.title,
        description=hit.description,
        license=hit.license,
        state=hit.state,
        updated_at=hit.updated_at,
    )


def _facet_values(buckets: list[FacetBucket]) -> list[FacetValue]:
    return [FacetValue(value=b.value, count=b.count) for b in buckets]


__all__ = [
    "FacetDimension",
    "FacetValue",
    "SearchItem",
    "SearchRequest",
    "SearchResponse",
    "SearchService",
]
