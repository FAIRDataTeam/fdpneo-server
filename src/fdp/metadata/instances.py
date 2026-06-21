"""Dynamic instance / subclass lookup for DASH reference form widgets.

Backs the reference editors (``dash:AutoCompleteEditor``,
``dash:InstancesSelectEditor``, ``dash:SubClassEditor``): a curator picks an IRI
value by browsing the *instances of a class* (the class comes from the property
shape's ``sh:class``). This is distinct from ``/forms/autocomplete`` (TASKS 10.6),
which serves admin-curated named sources — here the lookup is dynamic, keyed by a
class IRI, over whatever lives in this FDP's graph.

* ``GET /fdp-api/instances?class=<C>&q=&limit=&offset=`` — instances of class
  ``C`` as ``{iri, label, type}``, optional case-insensitive ``q`` filter, paged
  with the ``X-FDP-Page-*`` headers used by ``/page`` (TASKS 10.9).
* ``GET /fdp-api/subclasses?class=<C>`` — the transitive ``rdfs:subClassOf``
  descendants of ``C`` as ``{iri, label}``.

**Scope (v1).** "Instances of ``C``" means resources that exist as *records* in
this FDP and are typed ``C`` — one graph per record (ADR-0007), so the query is
``GRAPH ?s { ?s a <C> }``. This naturally excludes FDP machinery graphs and
entities described inline inside another record; surfacing embedded entities is a
later increment. No external federation.

**Visibility.** Every returned instance is gated exactly like every other read
(mirrors :func:`fdp.metadata.extensions._read_authorised`): the ODRL ``read``
decision must permit, and the publication-state gate must pass — so an anonymous
caller sees published instances only. Gating is per-item on the page (drops
hidden rows), which is why a page can be shorter than ``limit``; the
``X-FDP-Page-Total`` is the pre-gate candidate count, consistent with ``/page``.

Subclasses are vocabulary terms, not records, so they are not state-gated.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from fdp.identity.deps import current_context
from fdp.policy.model import Action, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest
from fdp.shared.sparql_safety import sparql_string_literal

if TYPE_CHECKING:
    from fdp.metadata.lifecycle import StateGate
    from fdp.policy.pdp import PDP
    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_SPARQL_JSON: Final = "application/sparql-results+json"
_DEFAULT_LIMIT: Final = 20
_MAX_LIMIT: Final = 100

# Label predicates, in priority order (skos:prefLabel → rdfs:label → dct:title
# → foaf:name); the first bound one wins, else the IRI's short form.
_LABEL_IRIS: Final = (
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://purl.org/dc/terms/title",
    "http://xmlns.com/foaf/0.1/name",
)
_RDF_TYPE: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_SUBCLASS_OF: Final = "http://www.w3.org/2000/01/rdf-schema#subClassOf"

# A conservative absolute-IRI check for the user-supplied class — embedded as
# ``<...>`` so it must not carry IRI-breaking characters.
_IRI_RE: Final = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s<>\"{}|\\^`]+$")


# --- response models -------------------------------------------------------


class InstanceItem(BaseModel):
    iri: str
    label: str
    type: str


class InstanceListView(BaseModel):
    items: list[InstanceItem]


class SubclassItem(BaseModel):
    iri: str
    label: str


class SubclassListView(BaseModel):
    items: list[SubclassItem]


# --- service ---------------------------------------------------------------


class InstanceLookupService:
    """Enumerates class instances / subclasses, gated by read visibility.

    Stateless aside from its collaborators; safe to share across requests.
    """

    def __init__(
        self,
        *,
        adapter: TripleStoreAdapter,
        pdp: PDP,
        base_url: str,
        state_gate: StateGate | None = None,
    ) -> None:
        self._adapter = adapter
        self._pdp = pdp
        self._base = base_url.rstrip("/")
        self._state_gate = state_gate

    async def instances(
        self,
        *,
        class_iri: str,
        q: str | None,
        limit: int,
        offset: int,
        ctx: RequestContext,
    ) -> tuple[list[InstanceItem], int]:
        """Return one page of visible instances of ``class_iri`` plus the total."""
        cls = _require_iri(class_iri)
        where = _instances_where(cls, q)
        total = await self._count(where)
        if total == 0:
            return [], 0
        rows = await self._select(
            f"SELECT ?s (SAMPLE(?lbl) AS ?label) WHERE {{{where}}}"
            f" GROUP BY ?s ORDER BY ?s LIMIT {int(limit)} OFFSET {int(offset)}"
        )
        items: list[InstanceItem] = []
        for row in rows:
            iri = row.get("s", {}).get("value")
            if iri is None or not await self._visible(ctx, iri):
                continue
            items.append(InstanceItem(iri=iri, label=_label_of(row, iri), type=cls))
        return items, total

    async def subclasses(self, *, class_iri: str) -> list[SubclassItem]:
        """Return the transitive ``rdfs:subClassOf`` descendants of ``class_iri``."""
        cls = _require_iri(class_iri)
        where = (
            f" GRAPH ?g {{ ?s <{_SUBCLASS_OF}>+ <{cls}> ."
            + "".join(f" OPTIONAL {{ ?s <{p}> ?l{i} }}" for i, p in enumerate(_LABEL_IRIS))
            + f" BIND(COALESCE({', '.join(f'?l{i}' for i in range(len(_LABEL_IRIS)))}) AS ?lbl) }}"
        )
        rows = await self._select(
            f"SELECT ?s (SAMPLE(?lbl) AS ?label) WHERE {{{where}}} GROUP BY ?s ORDER BY ?s"
        )
        out: list[SubclassItem] = []
        for row in rows:
            iri = row.get("s", {}).get("value")
            if iri is None or iri == cls:
                continue
            out.append(SubclassItem(iri=iri, label=_label_of(row, iri)))
        return out

    # --- internals ---------------------------------------------------------

    async def _visible(self, ctx: RequestContext, iri: str) -> bool:
        """ODRL read + publication-state gate, matching every other read path."""
        decision = await self._pdp.authorize(ctx, Action.READ, iri)
        if decision.outcome is not Outcome.PERMIT:
            return False
        if self._state_gate is not None:
            return await self._state_gate.is_visible(ctx, iri)
        return True

    async def _count(self, where: str) -> int:
        rows = await self._select(f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{{where}}}")
        for row in rows:
            raw = row.get("n", {}).get("value")
            if raw is not None:
                try:
                    return int(raw)
                except ValueError:
                    return 0
        return 0

    async def _select(self, query: str) -> list[dict[str, dict[str, str]]]:
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        payload = json.loads(body)
        return payload.get("results", {}).get("bindings", [])


# --- pure helpers ----------------------------------------------------------


def _require_iri(value: str) -> str:
    if not _IRI_RE.match(value):
        raise BadRequest(
            "`class` must be an absolute http(s) IRI",
            details={"class": value},
        )
    return value


def _instances_where(cls: str, q: str | None) -> str:
    """The shared WHERE body for the count + page queries (records typed ``cls``)."""
    labels = "".join(f" OPTIONAL {{ ?s <{p}> ?l{i} }}" for i, p in enumerate(_LABEL_IRIS))
    coalesce = ", ".join(f"?l{i}" for i in range(len(_LABEL_IRIS)))
    body = f" GRAPH ?s {{ ?s <{_RDF_TYPE}> <{cls}> .{labels} BIND(COALESCE({coalesce}) AS ?lbl) }}"
    if q:
        needle = sparql_string_literal(q.lower())
        body += f" FILTER(CONTAINS(LCASE(STR(COALESCE(?lbl, STR(?s)))), {needle}))"
    return body


def _label_of(row: dict[str, dict[str, str]], iri: str) -> str:
    """Resolved label for a binding row, or the IRI's short form as a fallback."""
    label = row.get("label", {}).get("value")
    if label:
        return label
    tail = re.split(r"[#/]", iri.rstrip("#/"))[-1]
    return tail or iri


# --- router ----------------------------------------------------------------


def build_instances_router(*, service: InstanceLookupService) -> APIRouter:
    """Build the instance/subclass lookup router. Reads are visibility-gated."""
    router = APIRouter(tags=["instances"])

    @router.get("/instances", response_model=InstanceListView, name="instances_list")
    async def list_instances(  # pyright: ignore[reportUnusedFunction]
        response: Response,
        ctx: Annotated[RequestContext, Depends(current_context)],
        class_: Annotated[str, Query(alias="class", description="Class IRI to enumerate.")],
        q: Annotated[str | None, Query(description="Case-insensitive label/IRI filter.")] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> InstanceListView:
        """Published instances of ``class`` as ``{iri, label, type}`` (auth/state gated)."""
        items, total = await service.instances(
            class_iri=class_, q=q, limit=limit, offset=offset, ctx=ctx
        )
        response.headers["X-FDP-Page-Total"] = str(total)
        response.headers["X-FDP-Page-Offset"] = str(offset)
        response.headers["X-FDP-Page-Limit"] = str(limit)
        return InstanceListView(items=items)

    @router.get("/subclasses", response_model=SubclassListView, name="subclasses_list")
    async def list_subclasses(  # pyright: ignore[reportUnusedFunction]
        class_: Annotated[str, Query(alias="class", description="Root class IRI.")],
    ) -> SubclassListView:
        """The transitive ``rdfs:subClassOf`` descendants of ``class``."""
        return SubclassListView(items=await service.subclasses(class_iri=class_))

    return router


__all__ = [
    "InstanceItem",
    "InstanceListView",
    "InstanceLookupService",
    "SubclassItem",
    "SubclassListView",
    "build_instances_router",
]
