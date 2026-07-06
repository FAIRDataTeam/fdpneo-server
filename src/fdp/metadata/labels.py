"""Labels endpoint (task 6.1).

Resolves a batch of IRIs to their best human-readable label. The
``fdp-client`` calls this to render a ``dct:license`` URL as
"Creative Commons Attribution 4.0", a ``foaf:Organization`` IRI as the
publisher's name, and so on.

Public — labels are descriptive metadata terms. The PDP gates the full
record graph but not snippet labels. This matches the reference Java
implementation's behaviour.

Lookup strategy:

1. Per-``(iri, language)`` TTL cache. Negative misses are cached too so
   the same unknown IRI doesn't re-query the triple store on every
   client call.
2. Cache miss → batched SPARQL query against ``GRAPH ?g``. Searches
   ``rdfs:label``, ``skos:prefLabel`` and ``dcterms:title`` (the same
   predicate set the reference impl uses).
3. Pick the best label per IRI by ``(language_score, predicate_score)``:
   requested-language wins over no-language-tag wins over any other
   language; ``rdfs:label`` wins over ``skos:prefLabel`` wins over
   ``dcterms:title`` within a language band.
4. Secondary source (6.1a): IRIs the graph doesn't describe — typically
   external vocabulary terms like ``dct:license`` or MIME types — fall back
   to the curated ``forms.autocomplete-sources`` setting, whose inline items
   map those exact IRIs to labels. The graph always wins; the inline map only
   fills gaps and is language-neutral.
5. Third source (Phase 21, ADR-0012 extension): external IRIs the graph and
   inline map don't describe (a ROR org, a DOI, an ORCID, a SKOS term) are
   dereferenced over content-negotiated RDF and cached in Postgres. Off by
   default; only allow-listed hosts are fetched (see
   :class:`fdp.config.RemoteLabelSettings` and
   :mod:`fdp.metadata.external_labels`). Lazy by default — a first-seen external
   IRI is resolved in the background and returned on a later call — with an
   opt-in bounded ``?wait`` for inline resolution.

Security:

* Caller-supplied IRIs are validated against an allow-set of legal IRI
  characters before any interpolation into the SPARQL query
  (CLAUDE.md "SPARQL strings are parsed, never interpolated" —
  honoured by rejecting anything that contains the few characters
  SPARQL syntax actually treats specially). Malformed IRIs are
  silently dropped rather than 400'd; a partial response is more
  useful than a hard failure on a mixed batch.
* The endpoint is unauthenticated. Do not surface labels for resources
  that would themselves be access-controlled — but the labels we serve
  are vocabulary terms that anyone who knows the IRI already knows the
  meaning of.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Annotated, Any, Final
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel

from fdp.metadata.settings import AutocompleteSources
from fdp.shared.errors import BadRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fdp.config import RemoteLabelSettings
    from fdp.metadata.external_labels import ExternalLabelCache, ExternalLabelFetcher
    from fdp.metadata.settings import SettingsRepository
    from fdp.storage.triplestore.adapter import TripleStoreAdapter


log = structlog.get_logger(__name__)

# Settings key holding the curated autocomplete sources (Phase 9.3). Its inline
# items map vocabulary IRIs (licenses, MIME types, …) to human labels and serve
# as the secondary label source (6.1a) for IRIs the knowledge graph doesn't
# describe.
_AUTOCOMPLETE_KEY: Final = "forms.autocomplete-sources"


# Label predicates queried, in precedence order. A literal matched by
# ``rdfs:label`` wins over the same literal under ``skos:prefLabel``
# wins over ``dcterms:title`` when comparing within one language band.
_LABEL_PREDICATES: Final = (
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "http://purl.org/dc/terms/title",
)


# Characters that must NOT appear inside an IRI literal in SPARQL —
# they would either be syntactically meaningful or break out of the
# ``<...>`` IRI delimiter. The list mirrors the IRI grammar from
# SPARQL 1.1 §19.8.
_FORBIDDEN_IRI_CHARS: Final = frozenset(' \t\n\r<>"{}|^`\\')


# --- response model --------------------------------------------------------


class LabelsResponse(BaseModel):
    """Response shape for ``GET /labels``."""

    labels: dict[str, str]
    """``{iri: label}``. IRIs with no discoverable label are omitted."""


# --- cache -----------------------------------------------------------------


class _Sentinel:
    __slots__ = ()


_MISS: Final = _Sentinel()


class _LabelCache:
    """In-memory ``(iri, language) → label or None`` TTL cache.

    Stores both positive hits (label string) and negative ones (``None``)
    so a missing label doesn't re-query the triple store for the
    cache's lifetime. ``time.monotonic`` is used rather than
    ``time.time`` so the cache survives wall-clock jumps.
    """

    __slots__ = ("_data", "_ttl")

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = float(ttl_seconds)
        self._data: dict[tuple[str, str], tuple[float, str | None]] = {}

    def get(self, iri: str, language: str) -> str | None | _Sentinel:
        entry = self._data.get((iri, language))
        if entry is None:
            return _MISS
        expiry, label = entry
        if expiry <= time.monotonic():
            del self._data[(iri, language)]
            return _MISS
        return label

    def set(self, iri: str, language: str, label: str | None) -> None:
        self._data[(iri, language)] = (time.monotonic() + self._ttl, label)

    def __len__(self) -> int:
        return len(self._data)


# --- resolver --------------------------------------------------------------


class LabelResolver:
    """Resolves IRIs to labels via the local triple store + curated sources.

    Primary source is the knowledge graph (record/instance labels). A secondary,
    settings-backed source (6.1a) fills IRIs the graph doesn't describe — the
    inline ``forms.autocomplete-sources`` items map vocabulary IRIs (licenses,
    MIME types, …) to labels. The graph always wins; the inline map only fills
    gaps and is treated as language-neutral.

    Stateful only via the caches; safe to share across requests.
    """

    def __init__(
        self,
        *,
        adapter: TripleStoreAdapter,
        settings_repository: SettingsRepository | None = None,
        external_cache: ExternalLabelCache | None = None,
        external_fetcher: ExternalLabelFetcher | None = None,
        remote_settings: RemoteLabelSettings | None = None,
        cache_ttl_seconds: int = 3600,
        inline_ttl_seconds: int = 300,
        max_iris_per_query: int = 100,
    ) -> None:
        self._adapter = adapter
        self._settings_repository = settings_repository
        self._cache = _LabelCache(cache_ttl_seconds)
        self._max_iris_per_query = max_iris_per_query
        self._inline_ttl = float(inline_ttl_seconds)
        # (map, expiry-monotonic). Refreshed lazily so an admin edit to the
        # autocomplete sources shows up within the TTL without a restart.
        self._inline_cache: tuple[dict[str, str], float] | None = None
        # Third source (Phase 21): external IRI dereferencing. All three must be
        # wired and the settings ``effective_enabled`` for it to run.
        self._external_cache = external_cache
        self._external_fetcher = external_fetcher
        self._remote_settings = remote_settings
        # Background cache-warming tasks + a stampede guard so concurrent lookups
        # of the same first-seen IRI dereference it only once.
        self._bg_tasks: set[asyncio.Task[str | None]] = set()
        self._inflight: set[tuple[str, str]] = set()

    @property
    def _external_enabled(self) -> bool:
        return (
            self._external_cache is not None
            and self._external_fetcher is not None
            and self._remote_settings is not None
            and self._remote_settings.effective_enabled
        )

    async def lookup(
        self, iris: Sequence[str], *, language: str, wait_ms: int = 0
    ) -> dict[str, str]:
        """Resolve ``iris`` to labels in ``language`` (with fallbacks).

        Order: in-memory cache → knowledge graph → curated inline sources →
        external dereferencing (Phase 21, when enabled). Returns only IRIs that
        have a discoverable label. Cached negatives are honoured.

        External resolution is lazy: a first-seen external IRI is fetched in the
        background and this call omits it; a later call returns it from cache.
        ``wait_ms`` opts into a bounded blocking wait (capped by the configured
        ``max_wait_ms``) so a caller can get the label inline on the first ask.
        """
        result: dict[str, str] = {}
        to_query: list[str] = []
        for iri in iris:
            value = self._cache.get(iri, language)
            if isinstance(value, _Sentinel):
                to_query.append(iri)
                continue
            if value is not None:
                result[iri] = value

        if not to_query:
            return result

        inline = await self._inline_labels()
        external_candidates: list[str] = []
        # Query in chunks to keep individual queries bounded.
        for start in range(0, len(to_query), self._max_iris_per_query):
            batch = to_query[start : start + self._max_iris_per_query]
            resolved = await self._query_batch(batch, language=language)
            for iri in batch:
                # Knowledge graph first, curated inline source as a fallback.
                label = resolved.get(iri)
                if label is None:
                    label = inline.get(iri)
                if label is not None:
                    self._cache.set(iri, language, label)
                    result[iri] = label
                elif self._external_enabled:
                    # Defer the negative — the external path decides and caches.
                    external_candidates.append(iri)
                else:
                    self._cache.set(iri, language, None)

        if external_candidates:
            await self._resolve_external(
                external_candidates, language=language, wait_ms=wait_ms, result=result
            )
        return result

    async def _query_batch(self, iris: Sequence[str], *, language: str) -> dict[str, str | None]:
        sparql = _build_sparql(iris)
        body = await self._adapter.query(sparql)
        return _pick_best_labels(body, requested_language=language)

    # --- external resolution (Phase 21) -----------------------------------

    async def _resolve_external(
        self, iris: Sequence[str], *, language: str, wait_ms: int, result: dict[str, str]
    ) -> None:
        """Resolve external IRIs via the Postgres cache, then a bounded fetch.

        Mutates ``result`` in place with any labels learned in time. Assumes the
        external collaborators are wired (guarded by ``_external_enabled``).
        """
        assert self._external_cache is not None
        assert self._remote_settings is not None

        # 1. Durable cache — a hit (positive or cached-miss) short-circuits and
        #    also seeds the in-memory hot layer.
        cached = await self._external_cache.get_many(iris, language=language)
        still_unknown: list[str] = []
        for iri in iris:
            if iri in cached:
                label = cached[iri]
                self._cache.set(iri, language, label)
                if label is not None:
                    result[iri] = label
            else:
                still_unknown.append(iri)

        # 2. Dereference the rest — only allow-listed hosts are eligible.
        eligible = [iri for iri in still_unknown if self._eligible(iri)]
        tasks: dict[asyncio.Task[str | None], str] = {}
        for iri in eligible:
            if (iri, language) in self._inflight:
                continue  # already being fetched — don't stampede the remote
            self._inflight.add((iri, language))
            task = asyncio.create_task(self._fetch_and_persist(iri, language))
            self._bg_tasks.add(task)
            task.add_done_callback(self._on_task_done)
            tasks[task] = iri

        # 3. Lazy by default (return now, warm the cache); opt-in bounded wait.
        if wait_ms > 0 and tasks:
            deadline = min(wait_ms, self._remote_settings.max_wait_ms) / 1000
            done, _pending = await asyncio.wait(list(tasks), timeout=deadline)
            for task in done:
                label = task.result()
                if label is not None:
                    result[tasks[task]] = label
            # Unfinished fetches keep running to warm the cache for next time.

    def _eligible(self, iri: str) -> bool:
        """True iff ``iri`` is safe and its host is on the allow-list."""
        if self._remote_settings is None or not is_safe_iri(iri):
            return False
        return (urlsplit(iri).hostname or "") in self._remote_settings.hosts

    async def _fetch_and_persist(self, iri: str, language: str) -> str | None:
        """Dereference ``iri``, persist the (positive or negative) result, cache it."""
        assert self._external_fetcher is not None
        assert self._external_cache is not None
        assert self._remote_settings is not None
        try:
            label = await self._external_fetcher.fetch(iri, language=language)
            ttl = (
                self._remote_settings.positive_ttl_seconds
                if label is not None
                else self._remote_settings.negative_ttl_seconds
            )
            try:
                await self._external_cache.upsert(
                    iri,
                    language,
                    label,
                    ttl_seconds=ttl,
                    source_host=urlsplit(iri).hostname,
                )
            except Exception as err:  # persistence is best-effort
                log.warning("external_label_persist_failed", iri=iri, error=repr(err))
            self._cache.set(iri, language, label)  # in-memory hot layer
            return label
        finally:
            self._inflight.discard((iri, language))

    def _on_task_done(self, task: asyncio.Task[str | None]) -> None:
        self._bg_tasks.discard(task)
        # Consume any exception so a background fetch never logs "never retrieved".
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.result()

    async def shutdown(self) -> None:
        """Cancel outstanding background fetches (called on app shutdown)."""
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._bg_tasks.clear()
        self._inflight.clear()

    async def _inline_labels(self) -> dict[str, str]:
        """``iri -> label`` from the inline autocomplete sources (TTL-cached).

        Empty when no settings repository is wired (keeps the resolver usable
        with the triple store alone). Best-effort: a settings hiccup degrades
        to graph-only resolution rather than failing the request.
        """
        if self._settings_repository is None:
            return {}
        now = time.monotonic()
        cached = self._inline_cache
        if cached is not None and cached[1] > now:
            return cached[0]
        mapping: dict[str, str] = {}
        try:
            sources = await self._settings_repository.read_with_default(_AUTOCOMPLETE_KEY)
        except Exception as err:  # pragma: no cover - defensive
            log.warning("label_inline_sources_unavailable", error=repr(err))
            sources = None
        if isinstance(sources, AutocompleteSources):
            for source in sources.sources:
                if source.kind != "inline":
                    continue
                for item in source.items:
                    if item.iri and item.label:
                        mapping.setdefault(item.iri, item.label)
        self._inline_cache = (mapping, now + self._inline_ttl)
        return mapping


# --- pure helpers ----------------------------------------------------------


def is_safe_iri(iri: str) -> bool:
    """Return True iff ``iri`` is a plausible IRI safe to inline into SPARQL.

    The check is conservative: an IRI is rejected if it contains
    whitespace, the ``<>`` brackets used to delimit IRIs in SPARQL, the
    quote char, or any C0 control character. These are the same
    characters disallowed by the SPARQL 1.1 IRI grammar, so a string
    that fails this check is not a legal IRI anyway.
    """
    if not iri or len(iri) > 2048:
        return False
    if any(c in _FORBIDDEN_IRI_CHARS for c in iri):
        return False
    return not any(ord(c) < 0x20 for c in iri)


def _build_sparql(iris: Sequence[str]) -> str:
    """Build the batched ``SELECT`` over the cross product of IRIs and predicates.

    No user-supplied content is interpolated raw: every IRI must pass
    :func:`is_safe_iri` before reaching here (the caller is the router
    handler, which filters first), and the predicate list is a fixed
    module constant. ``json.dumps`` is used for the literal language
    tag in case someone ever passes an unusual codepoint, but that
    string is never interpolated.
    """
    iri_values = " ".join(f"<{iri}>" for iri in iris)
    predicate_values = " ".join(f"<{p}>" for p in _LABEL_PREDICATES)
    return (
        "SELECT ?iri ?p ?label WHERE {\n"
        f"  VALUES ?iri {{ {iri_values} }}\n"
        f"  VALUES ?p {{ {predicate_values} }}\n"
        "  GRAPH ?g { ?iri ?p ?label }\n"
        "  FILTER(isLiteral(?label))\n"
        "}\n"
    )


def _pick_best_labels(sparql_json_body: bytes, *, requested_language: str) -> dict[str, str | None]:
    """Reduce raw SPARQL JSON results to ``{iri: label-or-None}``.

    Scoring per row:

    * ``language_score`` — 0 for the requested language, 1 for the
      empty (no-language-tag) literal, 2 for any other language.
    * ``predicate_score`` — index into :data:`_LABEL_PREDICATES`; lower
      is better.

    The row with the lowest ``(language_score, predicate_score)`` tuple
    wins per IRI. IRIs with no matching literal map to ``None`` so the
    cache can record the negative result.
    """
    payload: dict[str, Any] = json.loads(sparql_json_body)
    bindings: list[dict[str, Any]] = payload.get("results", {}).get("bindings", [])

    best: dict[str, tuple[int, int, str]] = {}
    seen: set[str] = set()
    for row in bindings:
        iri = row.get("iri", {}).get("value")
        predicate = row.get("p", {}).get("value")
        label_term: dict[str, Any] = row.get("label", {})
        label = label_term.get("value")
        lang = label_term.get("xml:lang", "")
        if not iri or not predicate or not label:
            continue
        seen.add(iri)
        if lang == requested_language:
            language_score = 0
        elif lang == "":
            language_score = 1
        else:
            language_score = 2
        try:
            predicate_score = _LABEL_PREDICATES.index(predicate)
        except ValueError:
            predicate_score = len(_LABEL_PREDICATES)
        candidate = (language_score, predicate_score, label)
        existing = best.get(iri)
        if existing is None or candidate < existing:
            best[iri] = candidate

    # Negative results: IRIs that were queried but matched no literal
    # should be returned with None so the cache records the miss.
    # Callers compute the negative set from the input list, not from
    # this function, so we only return what we positively learned.
    return {iri: label for iri, (_, _, label) in best.items()}


# --- router ----------------------------------------------------------------


def build_labels_router(
    *,
    resolver: LabelResolver,
    default_language: str = "en",
    max_iris_per_request: int = 100,
    max_wait_ms: int = 3000,
) -> APIRouter:
    """Construct ``GET /labels``.

    ``default_language`` is what the endpoint uses when the client
    doesn't pass ``?lang=``. ``max_iris_per_request`` is a hard cap to
    protect the triple store from a degenerate single-call batch.
    ``max_wait_ms`` bounds the opt-in ``?wait`` (external resolution).
    """
    router = APIRouter(tags=["labels"])

    @router.get("/labels", response_model=LabelsResponse, name="labels_lookup")
    async def labels_lookup(  # pyright: ignore[reportUnusedFunction]
        iri: Annotated[
            list[str],
            Query(description="One or more IRIs to resolve. Repeat the parameter."),
        ],
        lang: Annotated[
            str,
            Query(
                description="Preferred BCP-47 language tag. Untagged literals are the fallback.",
                min_length=1,
                max_length=16,
            ),
        ] = default_language,
        # ``Query`` lives in the default (not ``Annotated``) so ``le=max_wait_ms``
        # — a runtime closure value — resolves despite ``from __future__
        # import annotations`` stringizing annotations.
        wait: int = Query(
            default=0,
            ge=0,
            le=max_wait_ms,
            description=(
                "Milliseconds to wait for external IRI resolution to complete "
                "inline. 0 (default) is lazy: unknown external IRIs are resolved "
                "in the background and returned on a later call."
            ),
        ),
    ) -> LabelsResponse:
        if not iri:
            return LabelsResponse(labels={})
        if len(iri) > max_iris_per_request:
            raise BadRequest(
                "too many IRIs in one request",
                details={"requested": len(iri), "max": max_iris_per_request},
            )
        # Silently drop malformed IRIs — like missing labels, they just
        # don't appear in the response. A partial response is more
        # useful than a hard 400 on a mixed batch.
        safe_iris = [i for i in iri if is_safe_iri(i)]
        if not safe_iris:
            return LabelsResponse(labels={})
        labels = await resolver.lookup(safe_iris, language=lang, wait_ms=wait)
        return LabelsResponse(labels=labels)

    return router


__all__ = [
    "LabelResolver",
    "LabelsResponse",
    "build_labels_router",
    "is_safe_iri",
]
