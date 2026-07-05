"""Rebuild the ``metadata_search`` projection from the triple store.

Shared by ``fdp search reindex`` and by ``fdp backup restore`` (the search index
is derived state, so it must be rebuilt after a bulk load — ADR-0016 §6). Walks
every non-internal named graph and re-derives its search row, including the
``anon_read`` visibility flag.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fdp.metadata.repository import MetadataRepository
from fdp.metadata.search.indexer import SearchIndexer
from fdp.metadata.search.repository import SearchIndexRepository
from fdp.policy.resolver import GraphBackedOfferResolver
from fdp.policy.runtime import RequestScopedPDP
from fdp.shared.graphs import is_internal_graph_uri

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.storage.triplestore import TripleStoreAdapter


async def reindex_all(
    adapter: TripleStoreAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    language: str,
    system_default_offer_iri: str | None = None,
) -> int:
    """Clear and rebuild the search index; return the number of records indexed.

    ``system_default_offer_iri`` is the offer records fall back to for anon-read
    evaluation, so inherited-public records are indexed as visible — pass the same
    value the runtime resolves (``resolve_runtime_state``).
    """
    repository = MetadataRepository(adapter)
    resolver = GraphBackedOfferResolver(
        repository, system_default_provider=lambda: system_default_offer_iri
    )
    pdp = RequestScopedPDP(session_factory=session_factory, offer_resolver=resolver)
    search_repo = SearchIndexRepository(session_factory=session_factory)
    indexer = SearchIndexer(
        records=repository,
        search=search_repo,
        pdp=pdp,
        language=language,
        enabled=True,
    )
    await search_repo.clear_all()
    body = await adapter.query(
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }",
        accept="application/sparql-results+json",
    )
    graphs = [
        b["g"]["value"] for b in json.loads(body).get("results", {}).get("bindings", []) if "g" in b
    ]
    count = 0
    for graph_iri in graphs:
        if is_internal_graph_uri(graph_iri):
            continue
        if await indexer.index(graph_iri):
            count += 1
    return count


__all__ = ["reindex_all"]
