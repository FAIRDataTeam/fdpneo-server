"""Metadata publication lifecycle — read gate + transition API (Phase 12, ADR-0010).

Three collaborators, all built on the publication-state triple
(``fdp:metadataState``) that :mod:`fdpneo_server.metadata.meta` stamps on a record's
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
from rdflib import Literal

from fdpneo_server.identity.deps import require_auth
from fdpneo_server.metadata.events import RecordStateChanged
from fdpneo_server.metadata.states import (
    MetadataState,
    allowed_transitions,
    transition_requires_admin,
)
from fdpneo_server.policy.model import Action, Outcome
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import Conflict, Forbidden, NotFound
from fdpneo_server.shared.graphs import (
    is_internal_graph_uri,
    meta_graph_uri,
    record_graph_uri,
    record_uri_from_sibling,
    state_record_iri,
)
from fdpneo_server.shared.namespaces import DCT, FDP_METADATA_STATE
from fdpneo_server.storage.triplestore.adapter import construct_named_graph

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from fdpneo_server.policy.runtime import RequestScopedPDP
    from fdpneo_server.shared.events import EventBus
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"
_SPARQL_JSON: Final = "application/sparql-results+json"
_NT: Final = "application/n-triples"


def _gate_subject(iri: str) -> str:
    """The record URI whose publication state gates visibility of ``iri``.

    A meta / audit / data sibling maps to its record; anything else is
    normalized by :func:`record_graph_uri`.
    """
    base = record_uri_from_sibling(iri)
    return str(base) if base is not None else str(record_graph_uri(iri))


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
        """Every non-internal record IRI whose meta state is ``PUBLISHED``.

        Used by the SPARQL projection as the candidate set for anonymous
        visibility. Scoped to ``…/meta`` graphs so only managed records match;
        FDP machinery records (resource definitions) are stripped.
        """
        return await self._state_bearing_records(
            f' ?record <{FDP_METADATA_STATE}> "{MetadataState.PUBLISHED.value}" }}'
        )

    async def all_record_graphs(self) -> set[str]:
        """Every non-internal record IRI that carries a publication state.

        The full LDP-managed record universe — the candidate set for the
        deterministic SPARQL projection (admins, and the MODIFY pass that
        makes a subject's own drafts visible).
        """
        return await self._state_bearing_records(f" ?record <{FDP_METADATA_STATE}> ?state }}")

    async def _state_bearing_records(self, pattern_tail: str) -> set[str]:
        rows = await self._select(
            "SELECT ?record WHERE { GRAPH ?meta {"
            + pattern_tail
            + ' FILTER(STRENDS(STR(?meta), "/meta")) }'
        )
        out: set[str] = set()
        for row in rows:
            iri = row.get("record", {}).get("value")
            if iri and not is_internal_graph_uri(iri):
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

        A sibling URI (``…/meta``, ``…/audit``, ``…/data``) is judged by its
        *record's* state — a published record's meta-metadata is exactly as
        visible as the record (issue #35: the gate used to look for the state
        of ``…/meta/meta``, which never exists, so anonymous meta reads always
        404'd and the advertised ``hasMetaMetadata`` link was dead for
        harvesters).
        """
        if not await self.is_visible(ctx, record_iri):
            raise NotFound(f"resource not found: {_gate_subject(record_iri)}")

    async def is_visible(self, ctx: RequestContext, record_iri: str) -> bool:
        """Non-raising form of :meth:`ensure_visible`."""
        record_iri = _gate_subject(record_iri)
        if await self._reader.is_published(record_iri):
            return True
        return await self._can_curate(ctx, record_iri)

    async def visible_read_graphs(self, ctx: RequestContext) -> set[str]:
        """The graphs ``ctx`` may read once publication state is layered on.

        Deterministic: the candidate sets come from the store (published
        records; for authenticated callers also their curatable drafts), and
        any candidate without a cached ODRL decision is evaluated — and
        cached — on the spot. The projection therefore never depends on what
        this subject happened to fetch over REST before (the pre-0.16 bug
        where a logged-in user saw *fewer* records than anonymous).

        Visible = read-permitted published graphs, plus (for authenticated
        callers) unpublished graphs they hold both ``modify`` and ``read``
        on — mirroring the REST rule in :meth:`ensure_visible`. Admins see
        every non-internal record graph, exactly as over REST.
        """
        if _ADMIN_ROLE in ctx.roles:
            return await self._reader.all_record_graphs()
        published = await self._reader.published_graphs()
        visible = await self._pdp.authorize_many(ctx, Action.READ, published)
        if not ctx.is_anonymous:
            unpublished = await self._reader.all_record_graphs() - published
            curatable = await self._pdp.authorize_many(ctx, Action.MODIFY, unpublished)
            if curatable:
                visible |= await self._pdp.authorize_many(ctx, Action.READ, curatable)
        return visible

    async def updatable_graphs(self, ctx: RequestContext) -> set[str]:
        """The graphs ``ctx`` may target with SPARQL Update — deterministic.

        Candidates are every managed record graph; admins get them all
        (matching the REST write PEPs' admin short-circuit), others the
        subset ODRL ``modify`` permits.
        """
        candidates = await self._reader.all_record_graphs()
        if _ADMIN_ROLE in ctx.roles:
            return candidates
        return await self._pdp.authorize_many(ctx, Action.MODIFY, candidates)

    async def update_read_scope(self, ctx: RequestContext) -> set[str]:
        """The WHERE-clause read scope for SPARQL Update — deterministic.

        The full ODRL-read set over all managed records, deliberately *not*
        narrowed by publication state (a writer's WHERE observes their full
        authorized-read set, as documented on the SPARQL router).
        """
        candidates = await self._reader.all_record_graphs()
        if _ADMIN_ROLE in ctx.roles:
            return candidates
        return await self._pdp.authorize_many(ctx, Action.READ, candidates)

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
        graph = await construct_named_graph(self._adapter, meta_uri)
        graph.set((subj, FDP_METADATA_STATE, Literal(to.value)))
        if self._clock is not None:
            graph.set((subj, DCT.modified, Literal(self._clock())))
        await self._adapter.replace_graph(meta_uri, graph.serialize(format="nt"), mime=_NT)


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
        # Managed resources (policies/licenses/…) live under the reserved prefix;
        # LDP records live at the root. ``state_record_iri`` resolves either.
        return await _transition(str(state_record_iri(base, path)), body, ctx)

    return router


__all__ = [
    "StateGate",
    "StateReader",
    "StateService",
    "StateTransitionRequest",
    "StateTransitionResponse",
    "TransitionResult",
    "allowed_transitions",
    "build_state_router",
]
