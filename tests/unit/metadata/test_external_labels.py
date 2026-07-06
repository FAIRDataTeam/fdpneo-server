"""Unit tests for external (remote) label resolution (Phase 21).

Grows across the phase; this first slice covers the ``RemoteLabelSettings``
configuration group (env parsing + the ``effective_enabled`` gate).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from rdflib import Graph
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fdp.config import RemoteLabelSettings
from fdp.metadata.external_labels import (
    ExternalLabelCache,
    ExternalLabelFetcher,
    ExternalLabelRow,
    best_label_from_graph,
)
from fdp.storage.postgres.models import Base, register_all_models

pytestmark = pytest.mark.unit


def _settings(**over: object) -> RemoteLabelSettings:
    return RemoteLabelSettings(_env_file=None, **over)  # type: ignore[arg-type]


def test_defaults_are_off_and_deny_all() -> None:
    s = _settings()
    assert s.enabled is False
    assert s.allowed_hosts == []
    assert s.hosts == frozenset()
    assert s.effective_enabled is False


def test_allowed_hosts_parses_csv() -> None:
    s = _settings(allowed_hosts="ror.org, doi.org , orcid.org")
    assert s.allowed_hosts == ["ror.org", "doi.org", "orcid.org"]
    assert s.hosts == frozenset({"ror.org", "doi.org", "orcid.org"})


def test_allowed_hosts_parses_json_array() -> None:
    s = _settings(allowed_hosts='["ror.org", "doi.org"]')
    assert s.allowed_hosts == ["ror.org", "doi.org"]


def test_effective_enabled_requires_switch_and_hosts() -> None:
    # Switch on but no hosts → still inert.
    assert _settings(enabled=True, allowed_hosts=[]).effective_enabled is False
    # Hosts listed but switch off → inert.
    assert _settings(enabled=False, allowed_hosts="ror.org").effective_enabled is False
    # Both → live.
    assert _settings(enabled=True, allowed_hosts="ror.org").effective_enabled is True


# --- ExternalLabelCache (SQLite variant) -----------------------------------

ROR = "https://ror.org/006hf6230"


@pytest.fixture
async def session_factory() -> Any:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_upsert_then_get_many_roundtrips(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", "University of Twente", ttl_seconds=3600, source_host="ror.org")
    got = await cache.get_many([ROR, "https://ror.org/unknown"], language="en")
    assert got == {ROR: "University of Twente"}


async def test_negative_result_is_cached_and_distinguishable(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", None, ttl_seconds=3600)
    got = await cache.get_many([ROR], language="en")
    # Present-with-None = cached miss; absent = unknown.
    assert ROR in got
    assert got[ROR] is None


async def test_language_is_part_of_the_key(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", "Twente", ttl_seconds=3600)
    assert await cache.get_many([ROR], language="nl") == {}


async def test_upsert_replaces_existing_row(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    await cache.upsert(ROR, "en", "old", ttl_seconds=3600)
    await cache.upsert(ROR, "en", "new", ttl_seconds=3600)
    assert await cache.get_many([ROR], language="en") == {ROR: "new"}


async def test_expired_rows_are_not_returned(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    # Write a pre-expired row directly.
    past = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory() as session:
        session.add(
            ExternalLabelRow(
                iri=ROR, language="en", label="stale", resolved_at=past, expires_at=past
            )
        )
        await session.commit()
    assert await cache.get_many([ROR], language="en") == {}


async def test_purge_expired_removes_only_stale(session_factory: Any) -> None:
    cache = ExternalLabelCache(session_factory=session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            ExternalLabelRow(
                iri="https://ror.org/live",
                language="en",
                label="live",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        session.add(
            ExternalLabelRow(
                iri="https://ror.org/dead",
                language="en",
                label="dead",
                resolved_at=now,
                expires_at=now - timedelta(hours=1),
            )
        )
        await session.commit()
    assert await cache.purge_expired() == 1
    assert await cache.get_many(["https://ror.org/live"], language="en") == {
        "https://ror.org/live": "live"
    }


# --- best_label_from_graph -------------------------------------------------

ORCID_IRI = "https://orcid.example/0000-0002-1825-0097"


def _graph(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def test_extract_prefers_requested_subject_and_predicate_rank() -> None:
    g = _graph(
        """
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix foaf: <http://xmlns.com/foaf/0.1/> .
        <https://orcid.example/0000-0002-1825-0097>
            rdfs:label "Josiah Carberry" ;
            foaf:name "J. Carberry" .
        """
    )
    # rdfs:label outranks foaf:name within the same language band.
    assert best_label_from_graph(g, ORCID_IRI, language="en") == "Josiah Carberry"


def test_extract_matches_scheme_and_dx_host_variants() -> None:
    # CrossRef describes the DOI under http://dx.doi.example while the request
    # used https://doi.example — normalization must bridge them, and must NOT
    # pick the decoy ISSN/journal title.
    g = _graph(
        """
        @prefix dct: <http://purl.org/dc/terms/> .
        <http://dx.doi.example/10.1/x> dct:title "The Work Title" .
        <https://id.cross.example/issn/1234> dct:title "Journal Name" .
        """
    )
    assert best_label_from_graph(g, "https://doi.example/10.1/x", language="en") == "The Work Title"


def test_extract_language_preference() -> None:
    g = _graph(
        """
        @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
        <https://voc.example/t1>
            skos:prefLabel "Aardappel"@nl , "Potato"@en , "Kartoffel"@de .
        """
    )
    assert best_label_from_graph(g, "https://voc.example/t1", language="nl") == "Aardappel"
    assert best_label_from_graph(g, "https://voc.example/t1", language="en") == "Potato"


def test_extract_untagged_beats_other_language() -> None:
    g = _graph(
        """
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <https://x.example/a> rdfs:label "Plain" , "Deutsch"@de .
        """
    )
    assert best_label_from_graph(g, "https://x.example/a", language="en") == "Plain"


def test_extract_ignores_labels_on_other_subjects() -> None:
    g = _graph(
        """
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <https://x.example/a#part> rdfs:label "Sub-resource" .
        """
    )
    # The fragment sub-resource must not answer for the fragment-less request.
    assert best_label_from_graph(g, "https://x.example/a", language="en") is None


# --- ExternalLabelFetcher (httpx.MockTransport) ----------------------------

_TTL_HEADERS = {"content-type": "text/turtle"}


def _fetcher(handler: Callable[[httpx.Request], httpx.Response], **over: object) -> Any:
    over.setdefault("enabled", True)
    over.setdefault("allowed_hosts", "orcid.example,doi.example,api.cross.example")
    settings = _settings(**over)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExternalLabelFetcher(http_client=client, settings=settings), client


@pytest.fixture(autouse=True)
def _no_ssrf_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the SSRF guard's DNS in functional tests (kept offline).

    The real guard is exercised separately below with a literal private IP.
    """

    async def _ok(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("fdp.metadata.external_labels.assert_public_url", _ok)


async def test_fetch_resolves_turtle_label() -> None:
    ttl = (
        "<https://orcid.example/0000-0002-1825-0097> "
        '<http://www.w3.org/2000/01/rdf-schema#label> "Josiah Carberry" .'
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ttl.encode(), headers=_TTL_HEADERS)

    fetcher, client = _fetcher(handler)
    async with client:
        got = await fetcher.fetch(ORCID_IRI, language="en")
    assert got == "Josiah Carberry"


async def test_fetch_follows_redirect_then_resolves() -> None:
    ttl = '<http://dx.doi.example/10.1/x> <http://purl.org/dc/terms/title> "Redirected Work" .'

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "doi.example":
            return httpx.Response(302, headers={"location": "https://api.cross.example/works/x"})
        return httpx.Response(200, content=ttl.encode(), headers=_TTL_HEADERS)

    fetcher, client = _fetcher(handler)
    async with client:
        got = await fetcher.fetch("https://doi.example/10.1/x", language="en")
    assert got == "Redirected Work"


async def test_fetch_skips_host_not_on_allow_list() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"", headers=_TTL_HEADERS)

    fetcher, client = _fetcher(handler)
    async with client:
        got = await fetcher.fetch("https://evil.example/x", language="en")
    assert got is None
    assert calls["n"] == 0  # never dereferenced


async def test_fetch_rejects_oversized_body() -> None:
    big = b"x" * 5000

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers=_TTL_HEADERS)

    fetcher, client = _fetcher(handler, max_bytes=1024)
    async with client:
        got = await fetcher.fetch(ORCID_IRI, language="en")
    assert got is None


async def test_fetch_returns_none_on_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    fetcher, client = _fetcher(handler)
    async with client:
        got = await fetcher.fetch(ORCID_IRI, language="en")
    assert got is None


async def test_fetch_returns_none_when_no_matching_label() -> None:
    ttl = '<https://other.example/z> <http://www.w3.org/2000/01/rdf-schema#label> "Elsewhere" .'

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ttl.encode(), headers=_TTL_HEADERS)

    fetcher, client = _fetcher(handler)
    async with client:
        got = await fetcher.fetch(ORCID_IRI, language="en")
    assert got is None


async def test_fetch_stops_after_too_many_redirects() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://doi.example/next"})

    fetcher, client = _fetcher(handler, max_redirects=2)
    async with client:
        got = await fetcher.fetch("https://doi.example/x", language="en")
    assert got is None


async def test_fetch_blocks_private_address_with_real_guard() -> None:
    # No monkeypatch bypass here: the real assert_public_url must block a literal
    # loopback even though the host is allow-listed.
    def handler(_req: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return httpx.Response(200, content=b"", headers=_TTL_HEADERS)

    settings = _settings(enabled=True, allowed_hosts="127.0.0.1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = ExternalLabelFetcher(http_client=client, settings=settings)
    async with client:
        got = await fetcher.fetch("http://127.0.0.1/x", language="en")
    assert got is None
