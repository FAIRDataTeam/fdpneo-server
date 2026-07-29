"""Unit tests for the search service: gating + facet-label config (Phase 7.2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from fdpneo_server.config import SearchSettings
from fdpneo_server.metadata.search.repository import FacetBucket, SearchQuery, SearchResult
from fdpneo_server.metadata.search.service import SearchRequest, SearchService
from fdpneo_server.metadata.settings import SearchFilter, SearchFilters
from fdpneo_server.shared.context import RequestContext

DCT_LICENSE = "http://purl.org/dc/terms/license"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


@dataclass
class _FakeRepo:
    result: SearchResult = field(default_factory=SearchResult)
    last_query: SearchQuery | None = None

    async def search(self, query: SearchQuery) -> SearchResult:
        self.last_query = query
        return self.result


@dataclass
class _FakeGate:
    visible: set[str] = field(default_factory=set)
    called: bool = False

    async def visible_read_graphs(self, ctx: RequestContext) -> set[str]:
        del ctx
        self.called = True
        return set(self.visible)


@dataclass
class _FakeSettingsRepo:
    filters: SearchFilters = field(default_factory=SearchFilters)

    async def read_with_default(self, key: str) -> SearchFilters:
        del key
        return self.filters


def _service(
    *,
    repo: _FakeRepo | None = None,
    gate: _FakeGate | None = None,
    settings_repo: _FakeSettingsRepo | None = None,
    settings: SearchSettings | None = None,
) -> SearchService:
    return SearchService(
        repository=repo or _FakeRepo(),  # type: ignore[arg-type]
        state_gate=gate or _FakeGate(),  # type: ignore[arg-type]
        settings_repository=settings_repo or _FakeSettingsRepo(),  # type: ignore[arg-type]
        settings=settings or SearchSettings(),
    )


def _anon() -> RequestContext:
    return RequestContext.anonymous(trace_id="t")


def _user() -> RequestContext:
    return RequestContext(subject="u", roles=frozenset(), trace_id="t")


@pytest.mark.unit
async def test_anonymous_query_is_public_only() -> None:
    repo, gate = _FakeRepo(), _FakeGate(visible={"http://x/draft"})
    svc = _service(repo=repo, gate=gate)
    await svc.search(_anon(), SearchRequest(query="genome"))
    assert repo.last_query is not None
    assert repo.last_query.anonymous is True
    assert repo.last_query.visible == ()
    assert gate.called is False  # no gate lookup for anonymous


@pytest.mark.unit
async def test_authenticated_query_passes_visible_set() -> None:
    repo = _FakeRepo()
    gate = _FakeGate(visible={"http://x/b", "http://x/a"})
    svc = _service(repo=repo, gate=gate)
    await svc.search(_user(), SearchRequest(query="x"))
    assert repo.last_query is not None
    assert repo.last_query.anonymous is False
    assert repo.last_query.visible == ("http://x/a", "http://x/b")  # sorted


@pytest.mark.unit
async def test_limit_is_clamped() -> None:
    repo = _FakeRepo()
    svc = _service(repo=repo, settings=SearchSettings(max_limit=50, default_limit=20))
    await svc.search(_anon(), SearchRequest(query="x", limit=500))
    assert repo.last_query is not None
    assert repo.last_query.limit == 50
    await svc.search(_anon(), SearchRequest(query="x"))
    assert repo.last_query.limit == 20


@pytest.mark.unit
async def test_facets_default_when_unconfigured() -> None:
    repo = _FakeRepo(
        result=SearchResult(
            facet_type=[FacetBucket("http://x/Catalog", 3)],
            facet_license=[FacetBucket("cc-by", 2)],
        )
    )
    resp = await _service(repo=repo).search(_anon(), SearchRequest())
    assert set(resp.facets) == {"type", "license"}
    assert resp.facets["type"].label == "Type"
    assert resp.facets["type"].values[0].value == "http://x/Catalog"
    assert resp.facets["type"].values[0].count == 3


@pytest.mark.unit
async def test_facets_follow_configured_filters() -> None:
    repo = _FakeRepo(result=SearchResult(facet_license=[FacetBucket("cc-by", 1)]))
    settings_repo = _FakeSettingsRepo(
        filters=SearchFilters(
            filters=[SearchFilter(name="lic", label="License Type", predicate=DCT_LICENSE)]
        )
    )
    resp = await _service(repo=repo, settings_repo=settings_repo).search(_anon(), SearchRequest())
    # Only the license dimension is exposed, with the configured label.
    assert set(resp.facets) == {"license"}
    assert resp.facets["license"].label == "License Type"
