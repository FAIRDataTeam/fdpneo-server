"""Metadata publication lifecycle — read gate + transition API (Phase 12, ADR-0010).

Three collaborators, all built on the publication-state triple
(``fdp:metadataState``) that :mod:`fdp.metadata.meta` stamps on a record's
``<record>/meta`` graph:

* :class:`StateReader` — reads state from the triple store (one record, or the
  whole published set for the SPARQL projection).
* :class:`StateGate` — the read-side visibility rule (ADR-0010 §2): ``PUBLISHED``
  is visible to anyone the ODRL policy already permits; ``DRAFT``/``ARCHIVED``
  only to the record owner (ODRL ``modify``) or an admin. Used by every read PEP.
* :class:`StateService` + :func:`build_state_router` — the
  ``POST /{record}/state`` transition surface and its state machine.

State is a *gate layered over* the ODRL decision, never folded into the authz
cache — see ADR-0010 for why. A transition therefore needs no cache
invalidation: it writes one meta triple and the next read re-reads state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from rdflib import Graph, Literal

from fdp.identity.deps import require_auth
from fdp.metadata.events import RecordStateChanged
from fdp.metadata.states import (
    MetadataState,
    transition_requires_admin,
)
from fdp.policy.model import Action, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.errors import Conflict, Forbidden, NotFound
from fdp.shared.graphs import meta_graph_uri, record_graph_uri
from fdp.shared.namespaces import DCT, FDP_METADATA_STATE

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from fdp.policy.runtime import RequestScopedPDP
    from fdp.shared.events import EventBus
    from fdp.storage.triplestore.adapter import TripleStoreAdapter

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_SPARQL_JSON: Final = "application/sparql-results+json"
_NT: Final = "application/n-triples"


# --- state reader ----------------------------------------------------------


class StateReader:
    """Reads publication state from the meta graphs. Stateless; shareable."""

    def __init__(self, adapter: TripleStoreAdapter) -> None:
        self._adapter = adapter

    async def state_of(self, record_iri: str) -> MetadataState | None:
        """The record's current state, or ``None`` if it has none."""
        subj = str(record_graph_uri(record_iri))
        meta = str(meta_graph_uri(record_iri))
        rows = await self._select(
            f"SELECT ?s WHERE {{ GRAPH <{meta}> {{ <{subj}> <{FDP_METADATA_STATE}> ?s }} }}"
        )
        for row in rows:
            raw = row.get("s", {}).get("value")
            if raw is not None:
                try:
                    return MetadataState(raw)
                except ValueError:
                    return None
        return None

    async def is_published(self, record_iri: str) -> bool:
        """Fast path for the common read: is the record ``PUBLISHED``?"""
        subj = str(record_graph_uri(record_iri))
        meta = str(meta_graph_uri(record_iri))
        return await self._adapter.ask(
            f'ASK {{ GRAPH <{meta}> {{ <{subj}> <{FDP_METADATA_STATE}> "{MetadataState.PUBLISHED.value}" }} }}'
        )

    async def published_graphs(self) -> set[str]:
        """Every record IRI whose meta state is ``PUBLISHED``.

        Used by the SPARQL projection to intersect with the ODRL-permitted
        read set. Scoped to ``…/meta`` graphs so only managed records match.
        """
        rows = await self._select(
            "SELECT ?record WHERE { GRAPH ?meta {"
            f' ?record <{FDP_METADATA_STATE}> "{MetadataState.PUBLISHED.value}" }}'
            ' FILTER(STRENDS(STR(?meta), "/meta")) }'
        )
        out: set[str] = set()
        for row in rows:
            iri = row.get("record", {}).get("value")
            if iri:
                out.add(iri)
        return out

    async def _select(self, query: str) -> list[dict[str, dict[str, str]]]:
        body = await self._adapter.query(query, accept=_SPARQL_JSON)
        return json.loads(body).get("results", {}).get("bindings", [])


# --- read-side visibility gate ---------------------------------------------


class StateGate:
    """Applies the publication-state visibility rule at read PEPs (ADR-0010 §2)."""

    def __init__(self, *, reader: StateReader, pdp: RequestScopedPDP) -> None:
        self._reader = reader
        self._pdp = pdp

    async def ensure_visible(self, ctx: RequestContext, record_iri: str) -> None:
        """Raise :class:`NotFound` if ``ctx`` may not see the record's state.

        Assumes the ODRL ``read`` decision already permitted access; this only
        adds the state layer. ``PUBLISHED`` is visible to all; otherwise the
        caller must be an admin or hold ODRL ``modify`` (the owner). A hidden
        record 404s so its existence does not leak.
        """
        record_iri = str(record_graph_uri(record_iri))
        if await self._reader.is_published(record_iri):
            return
        if await self._can_curate(ctx, record_iri):
            return
        raise NotFound(f"resource not found: {record_iri}")

    async def is_visible(self, ctx: RequestContext, record_iri: str) -> bool:
        """Non-raising form of :meth:`ensure_visible`."""
        record_iri = str(record_graph_uri(record_iri))
        if await self._reader.is_published(record_iri):
            return True
        return await self._can_curate(ctx, record_iri)

    async def visible_read_graphs(self, ctx: RequestContext) -> set[str]:
        """The ODRL-read set narrowed to what ``ctx`` may see by state.

        Visible = the read-permitted set intersected with (published graphs
        union the subject's modify-permitted graphs). For an anonymous caller
        the modify set is empty, so this collapses to read-permitted-and-
        published. Replaces a bare ``authorized_graphs(ctx, READ)`` in the
        SPARQL endpoint.
        """
        read_set = await self._pdp.authorized_graphs(ctx, Action.READ)
        if not read_set:
            return read_set
        visible = read_set & await self._reader.published_graphs()
        if not ctx.is_anonymous:
            visible |= read_set & await self._pdp.authorized_graphs(ctx, Action.MODIFY)
        return visible

    async def _can_curate(self, ctx: RequestContext, record_iri: str) -> bool:
        if _ADMIN_ROLE in ctx.roles:
            return True
        if ctx.is_anonymous:
            return False
        decision = await self._pdp.authorize(ctx, Action.MODIFY, record_iri)
        return decision.outcome is Outcome.PERMIT


# --- transition service ----------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a successful state transition."""

    record_iri: str
    from_state: MetadataState
    to_state: MetadataState


class StateService:
    """Validates + applies ``POST /{record}/state`` transitions (ADR-0010 §3)."""

    def __init__(
        self,
        *,
        adapter: TripleStoreAdapter,
        reader: StateReader,
        pdp: RequestScopedPDP,
        event_bus: EventBus | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._reader = reader
        self._pdp = pdp
        self._bus = event_bus
        self._clock = clock

    async def transition(
        self, record_iri: str, *, to: MetadataState, ctx: RequestContext
    ) -> TransitionResult:
        """Move ``record_iri`` to state ``to`` on behalf of ``ctx``.

        Raises ``NotFound`` (no such managed record), ``Conflict`` (transition
        not in the state machine), or ``Forbidden`` (caller is neither admin
        nor owner / lacks admin for an admin-only transition).
        """
        record_iri = str(record_graph_uri(record_iri))
        current = await self._reader.state_of(record_iri)
        if current is None:
            raise NotFound(f"resource not found: {record_iri}")
        if current == to:
            raise Conflict(
                f"record is already {to.value}",
                details={"state": to.value},
            )
        requires_admin = transition_requires_admin(current, to)
        if requires_admin is None:
            raise Conflict(
                f"transition {current.value} → {to.value} is not allowed",
                details={"from": current.value, "to": to.value},
            )
        await self._authorize(ctx, record_iri, requires_admin=requires_admin)
        await self._write_state(record_iri, to)
        log.info(
            "record_state_changed",
            record=record_iri,
            from_state=current.value,
            to_state=to.value,
            subject=ctx.subject,
        )
        if self._bus is not None:
            await self._bus.publish(
                RecordStateChanged(
                    record_iri=record_iri,
                    subject=ctx.subject,
                    from_state=current.value,
                    to_state=to.value,
                    timestamp=ctx.request_timestamp,
                )
            )
        return TransitionResult(record_iri=record_iri, from_state=current, to_state=to)

    async def _authorize(
        self, ctx: RequestContext, record_iri: str, *, requires_admin: bool
    ) -> None:
        is_admin = _ADMIN_ROLE in ctx.roles
        if requires_admin:
            if not is_admin:
                raise Forbidden(
                    "this transition requires the admin role",
                    details={"required_role": _ADMIN_ROLE},
                )
            return
        if is_admin:
            return
        decision = await self._pdp.authorize(ctx, Action.MODIFY, record_iri)
        if decision.outcome is not Outcome.PERMIT:
            raise Forbidden(
                "only the record owner or an admin may change its state",
                details={"resource": record_iri},
            )

    async def _write_state(self, record_iri: str, to: MetadataState) -> None:
        """Swap the state triple (and bump ``dct:modified``) in the meta graph.

        A focused meta edit, not a record-content write: it does not change the
        record graph (so the content ETag is unaffected), bump
        ``owl:versionInfo``, or add a PROV Activity. The remaining required
        meta fields are preserved, so the graph stays schema-valid.
        """
        meta_uri = str(meta_graph_uri(record_iri))
        subj = record_graph_uri(record_iri)
        graph = await self._construct(meta_uri)
        graph.set((subj, FDP_METADATA_STATE, Literal(to.value)))
        if self._clock is not None:
            graph.set((subj, DCT.modified, Literal(self._clock())))
        await self._adapter.replace_graph(meta_uri, graph.serialize(format="nt"), mime=_NT)

    async def _construct(self, graph_uri: str) -> Graph:
        body = await self._adapter.query(
            f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}",
            accept="text/turtle",
        )
        graph = Graph()
        if body:
            graph.parse(data=body.decode("utf-8"), format="turtle")
        return graph


# --- router ----------------------------------------------------------------


class StateTransitionRequest(BaseModel):
    """Body for ``POST /{record}/state``."""

    to: MetadataState


class StateTransitionResponse(BaseModel):
    """Result of a transition."""

    record: str
    from_state: MetadataState
    to_state: MetadataState


def build_state_router(*, service: StateService, base_url: str) -> APIRouter:
    """Construct the transition router (``POST /state`` and ``/{path}/state``).

    Mounted **before** the LDP ``/{path:path}`` catch-all in ``main`` so the
    ``/state`` suffix isn't swallowed (same pattern as the read extensions).
    """
    router = APIRouter(tags=["lifecycle"])
    base = base_url.rstrip("/")

    async def _transition(
        record_iri: str, body: StateTransitionRequest, ctx: RequestContext
    ) -> StateTransitionResponse:
        result = await service.transition(record_iri, to=body.to, ctx=ctx)
        return StateTransitionResponse(
            record=result.record_iri,
            from_state=result.from_state,
            to_state=result.to_state,
        )

    @router.post("/state", response_model=StateTransitionResponse, name="state_root")
    async def root_state(  # pyright: ignore[reportUnusedFunction]
        body: StateTransitionRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> StateTransitionResponse:
        return await _transition(base + "/", body, ctx)

    @router.post(
        "/{path:path}/state", response_model=StateTransitionResponse, name="state_transition"
    )
    async def record_state(  # pyright: ignore[reportUnusedFunction]
        path: str,
        body: StateTransitionRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> StateTransitionResponse:
        return await _transition(f"{base}/{path}", body, ctx)

    return router


__all__ = [
    "StateGate",
    "StateReader",
    "StateService",
    "StateTransitionRequest",
    "StateTransitionResponse",
    "TransitionResult",
    "build_state_router",
]
