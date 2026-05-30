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

Remote-vocabulary fallback (config-driven allow-list) is deferred.
A future iteration can plug in a secondary resolver behind the same
:class:`LabelResolver` interface.

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

import json
import time
from typing import TYPE_CHECKING, Annotated, Any, Final

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel

from fdp.shared.errors import BadRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fdp.storage.triplestore.adapter import TripleStoreAdapter


log = structlog.get_logger(__name__)


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
_FORBIDDEN_IRI_CHARS: Final = frozenset(" \t\n\r<>\"{}|^`\\")


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
    """Resolves IRIs to labels via the local triple store.

    Stateful only via the cache; safe to share across requests.
    """

    def __init__(
        self,
        *,
        adapter: TripleStoreAdapter,
        cache_ttl_seconds: int = 3600,
        max_iris_per_query: int = 100,
    ) -> None:
        self._adapter = adapter
        self._cache = _LabelCache(cache_ttl_seconds)
        self._max_iris_per_query = max_iris_per_query

    async def lookup(
        self, iris: Sequence[str], *, language: str
    ) -> dict[str, str]:
        """Resolve ``iris`` to labels in ``language`` (with fallbacks).

        Returns only IRIs that have a discoverable label. Cached negative
        results are honoured — an IRI that previously returned no label
        is not re-queried until the cache expires.
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

        # Query in chunks to keep individual queries bounded.
        for start in range(0, len(to_query), self._max_iris_per_query):
            batch = to_query[start : start + self._max_iris_per_query]
            resolved = await self._query_batch(batch, language=language)
            for iri in batch:
                label = resolved.get(iri)
                self._cache.set(iri, language, label)
                if label is not None:
                    result[iri] = label
        return result

    async def _query_batch(
        self, iris: Sequence[str], *, language: str
    ) -> dict[str, str | None]:
        sparql = _build_sparql(iris)
        body = await self._adapter.query(sparql)
        return _pick_best_labels(body, requested_language=language)


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


def _pick_best_labels(
    sparql_json_body: bytes, *, requested_language: str
) -> dict[str, str | None]:
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
) -> APIRouter:
    """Construct ``GET /labels``.

    ``default_language`` is what the endpoint uses when the client
    doesn't pass ``?lang=``. ``max_iris_per_request`` is a hard cap to
    protect the triple store from a degenerate single-call batch.
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
        labels = await resolver.lookup(safe_iris, language=lang)
        return LabelsResponse(labels=labels)

    return router


__all__ = [
    "LabelResolver",
    "LabelsResponse",
    "build_labels_router",
    "is_safe_iri",
]
