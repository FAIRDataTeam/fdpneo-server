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

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import structlog
from rdflib import Graph, Literal, URIRef
from sqlalchemy import String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from fdp.metadata.labels import is_safe_iri
from fdp.shared import negotiation
from fdp.shared.errors import UpstreamError
from fdp.shared.namespaces import DCT, FOAF, RDFS, SDO, SKOS
from fdp.shared.ssrf import assert_public_url
from fdp.storage.postgres.models import Base
from fdp.storage.postgres.types import AwareDateTime

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.config import RemoteLabelSettings

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

    async def get_many(self, iris: Sequence[str], *, language: str) -> dict[str, str | None]:
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


# --- label extraction ------------------------------------------------------

# Predicates that carry a human label, in precedence order (lower index wins
# within a language band). ``rdfs:label``/``skos:prefLabel``/``dct:title`` mirror
# the local-graph resolver; ``foaf:name`` and ``schema:name`` (both http/https)
# are added because external person/organization descriptions favour them (an
# ORCID person carries ``foaf:name``; DOI work metadata carries ``dct:title``).
_LABEL_PREDICATES: tuple[URIRef, ...] = (
    RDFS.label,
    SKOS.prefLabel,
    DCT.title,
    FOAF.name,
    SDO.name,
    URIRef("http://schema.org/name"),
)

# Content-type → the media type understood by ``negotiation.parse`` (whose
# JSON-LD path enforces the remote-``@context`` SSRF guard, audit F-01/R-01).
_CONTENT_TYPE_TO_MEDIA: dict[str, str] = {
    "text/turtle": negotiation.TURTLE,
    "application/x-turtle": negotiation.TURTLE,
    "application/rdf+xml": negotiation.RDF_XML,
    "application/xml": negotiation.RDF_XML,
    "text/xml": negotiation.RDF_XML,
    "application/ld+json": negotiation.JSON_LD,
    "application/json": negotiation.JSON_LD,
}

_ACCEPT = "text/turtle, application/rdf+xml;q=0.9, application/ld+json;q=0.8"


def _normalize_iri(iri: str) -> str:
    """Scheme-insensitive key for matching a fetched subject to a requested IRI.

    Drops the scheme, a leading ``dx.``/``www.`` on the host, and a trailing
    slash, but keeps any fragment. This lets ``https://doi.org/10.1038/x`` match
    the ``http://dx.doi.org/10.1038/x`` subject CrossRef actually uses, while an
    ORCID ``#orcid-id`` sub-resource stays distinct from the person IRI.
    """
    parts = urlsplit(iri)
    host = (parts.hostname or "").lower()
    for prefix in ("dx.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    key = f"{host}{parts.path.rstrip('/')}"
    if parts.fragment:
        key = f"{key}#{parts.fragment}"
    return key


def best_label_from_graph(
    graph: Graph,
    iri: str,
    *,
    language: str,
    predicates: tuple[URIRef, ...] = _LABEL_PREDICATES,
) -> str | None:
    """Pick the best label for ``iri`` from a fetched description graph.

    Only literals hung off a subject that normalizes to ``iri`` are considered
    (so a DOI graph's journal/ISSN title can't be mistaken for the work's). The
    winner minimizes ``(language_band, predicate_rank)``: requested-language beats
    an untagged literal beats another language; within a band the earlier
    predicate in ``predicates`` wins.
    """
    target = _normalize_iri(iri)
    best: tuple[int, int, str] | None = None
    for rank, predicate in enumerate(predicates):
        for subject, obj in graph.subject_objects(predicate):
            if not isinstance(obj, Literal) or _normalize_iri(str(subject)) != target:
                continue
            lang = obj.language or ""
            band = 0 if lang == language else (1 if lang == "" else 2)
            candidate = (band, rank, str(obj))
            if best is None or candidate < best:
                best = candidate
    return best[2] if best is not None else None


# --- outbound fetcher ------------------------------------------------------


class ExternalLabelFetcher:
    """Dereferences an external IRI to a label via content-negotiated RDF.

    Off unless the resolver is ``effective_enabled``. Every fetch is allow-list
    gated and SSRF-guarded on *each* redirect hop, streamed with a size cap and a
    timeout, and parsed through :mod:`fdp.shared.negotiation` (which blocks remote
    JSON-LD ``@context`` fetches). A shared semaphore bounds concurrency.

    Generic RDF only: hosts that don't content-negotiate to RDF (e.g. ROR, whose
    label lives behind a JSON API) resolve to ``None`` — a per-source adapter for
    those is a deferred follow-up.
    """

    def __init__(self, *, http_client: httpx.AsyncClient, settings: RemoteLabelSettings) -> None:
        self._http = http_client
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_fetches)

    async def fetch(self, iri: str, *, language: str) -> str | None:
        """Resolve ``iri`` to a label, or ``None`` (not eligible / not found / failed).

        Never raises: a failed fetch is a cache miss, not a request error.
        """
        if not is_safe_iri(iri):
            return None
        if (urlsplit(iri).hostname or "") not in self._settings.hosts:
            return None  # not eligible — host isn't allow-listed
        async with self._semaphore:
            try:
                graph = await self._get_rdf(iri)
            except Exception as err:  # any failure → miss, never crash the caller
                log.info("external_label_fetch_failed", iri=iri, error=repr(err))
                return None
        return best_label_from_graph(graph, iri, language=language)

    async def _get_rdf(self, iri: str) -> Graph:
        """Fetch + parse ``iri``, following redirects with per-hop re-validation."""
        url = iri
        for _ in range(self._settings.max_redirects + 1):
            await assert_public_url(url, allowed_hosts=self._settings.hosts)
            body, content_type, redirect = await self._fetch_once(url)
            if redirect is not None:
                url = redirect
                continue
            return self._parse(body, content_type, base=url)
        raise UpstreamError("too many redirects resolving external label")

    async def _fetch_once(self, url: str) -> tuple[bytes, str, str | None]:
        """One hop: return ``(body, content_type, None)`` or ``(b"", "", redirect_url)``."""
        async with self._http.stream(
            "GET",
            url,
            timeout=self._settings.timeout_seconds,
            follow_redirects=False,
            headers={"Accept": _ACCEPT},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise UpstreamError("redirect without a Location header")
                return b"", "", str(response.url.join(location))
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._settings.max_bytes:
                    raise UpstreamError(
                        f"external document exceeds the {self._settings.max_bytes}-byte cap"
                    )
                chunks.append(chunk)
        return b"".join(chunks), content_type, None

    def _parse(self, body: bytes, content_type: str, *, base: str) -> Graph:
        """Parse ``body`` as RDF, preferring the declared media type then falling back.

        Goes through :func:`fdp.shared.negotiation.parse` so the JSON-LD path keeps
        the remote-``@context`` SSRF guard; tries Turtle / RDF-XML / JSON-LD in turn
        because a remote may set an unhelpful ``Content-Type``.
        """
        media = content_type.split(";", 1)[0].strip().lower()
        preferred = _CONTENT_TYPE_TO_MEDIA.get(media)
        candidates: list[str] = [preferred] if preferred else []
        candidates += [
            m
            for m in (negotiation.TURTLE, negotiation.RDF_XML, negotiation.JSON_LD)
            if m != preferred
        ]
        last_err: Exception | None = None
        for media_type in candidates:
            try:
                return negotiation.parse(body, media_type, base=base)
            except Exception as err:  # try the next candidate format
                last_err = err
                continue
        raise UpstreamError(f"external body is not parseable RDF: {last_err}")


__all__ = [
    "ExternalLabelCache",
    "ExternalLabelFetcher",
    "ExternalLabelRow",
    "best_label_from_graph",
]
