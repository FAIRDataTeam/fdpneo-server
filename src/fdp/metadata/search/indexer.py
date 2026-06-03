"""Search-index event subscriber (Phase 7.1).

Keeps ``metadata_search`` current by reacting to record-lifecycle events, the
same shape as :class:`fdp.metrics.pipeline.MetricsPipeline` and
:class:`fdp.metadata.audit.AuditLog`: ``start(bus)`` registers handlers and
retains the subscriptions; ``stop()`` drops them. Handler failures are logged
and swallowed — a search-index hiccup must never fail the write that produced
the event.

On create / modify / state-change it re-derives the row (including the
``anon_read`` visibility flag, evaluated once against the PDP for the anonymous
subject) and upserts; on delete it removes the row. Non-content records
(schemas, offers, resource definitions, internal graphs) are skipped.

**Freshness note (ADR-0010 / Phase 7):** ``anon_read`` is recomputed when a
record's *own* graph changes (its ``dct:rights``) or its state changes. A
change to an *inherited* offer (a parent's rights, or the system-default) does
not emit an event on the child, so a child's ``anon_read`` can lag until
``fdp search reindex`` — the same staleness class as the authorization cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from fdp.metadata.events import (
    RecordCreated,
    RecordDeleted,
    RecordModified,
    RecordStateChanged,
)
from fdp.metadata.search.extract import extract, is_indexable
from fdp.policy.model import Action, Outcome
from fdp.shared.context import RequestContext
from fdp.shared.graphs import record_graph_uri

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.search.repository import SearchIndexRepository
    from fdp.policy.runtime import RequestScopedPDP
    from fdp.shared.events import EventBus, Subscription

log = structlog.get_logger(__name__)


class SearchIndexer:
    """Subscribes to record events and maintains the search index."""

    __slots__ = ("_enabled", "_language", "_pdp", "_records", "_search", "_subs")

    def __init__(
        self,
        *,
        records: MetadataRepository,
        search: SearchIndexRepository,
        pdp: RequestScopedPDP,
        language: str,
        enabled: bool,
    ) -> None:
        self._records = records
        self._search = search
        self._pdp = pdp
        self._language = language
        self._enabled = enabled
        self._subs: list[Subscription] = []

    def start(self, bus: EventBus) -> None:
        """Register handlers for the record-lifecycle events. No-op when disabled."""
        if not self._enabled:
            log.info("search_indexer_disabled")
            return
        self._subs.extend(
            [
                bus.subscribe(RecordCreated, self._on_upsert),
                bus.subscribe(RecordModified, self._on_upsert),
                bus.subscribe(RecordStateChanged, self._on_upsert),
                bus.subscribe(RecordDeleted, self._on_deleted),
            ]
        )
        log.info("search_indexer_started")

    def stop(self) -> None:
        """Drop every bus subscription. Idempotent."""
        for sub in self._subs:
            sub.unsubscribe()
        self._subs.clear()

    # --- handlers ----------------------------------------------------------

    async def _on_upsert(self, event: RecordCreated | RecordModified | RecordStateChanged) -> None:
        await self._reindex_one(event.record_iri)

    async def _on_deleted(self, event: RecordDeleted) -> None:
        try:
            await self._search.delete(str(record_graph_uri(event.record_iri)))
        except Exception as err:
            log.warning("search_index_delete_failed", record=event.record_iri, error=repr(err))

    # --- core --------------------------------------------------------------

    async def index(self, record_iri: str) -> bool:
        """Index a single record by IRI. Returns True if a row was written.

        Shared by the event handlers and the reindex CLI. A record that is not
        indexable (internal/config) or has no content graph is removed from the
        index instead.
        """
        iri = str(record_graph_uri(record_iri))
        graph = await self._records.get_graph(iri)
        if len(graph) == 0 or not is_indexable(iri, graph):
            await self._search.delete(iri)
            return False
        meta = await self._records.get_meta(iri)
        record = extract(iri, graph, meta)
        anon_read = await self._anon_can_read(iri)
        await self._search.upsert(record, anon_read=anon_read, language=self._language)
        return True

    async def _reindex_one(self, record_iri: str) -> None:
        try:
            await self.index(record_iri)
        except Exception as err:
            log.warning("search_index_upsert_failed", record=record_iri, error=repr(err))

    async def _anon_can_read(self, record_iri: str) -> bool:
        anon = RequestContext.anonymous(trace_id="search-index")
        decision = await self._pdp.authorize(anon, Action.READ, record_iri)
        return decision.outcome is Outcome.PERMIT


__all__ = ["SearchIndexer"]
