"""Per-record graph CRUD on top of the triple store adapter.

**Responsibilities**

* Get / put / patch / delete the record graph identified by a record URI.
* On every mutation, refresh the sibling meta graph via
  :class:`MetaWriter`. The meta builder (see :mod:`fdp.metadata.meta`)
  owns the meta-graph shape, including the PROV ``Activity`` and the
  SHACL self-check.
* Compute the post-write ETag of the record graph so the LDP layer can
  return it in ``ETag`` headers for ``If-Match`` concurrency control.

**Non-responsibilities**

* SHACL validation of the *record* content — that lives in the LDP
  router via :class:`ShaclValidator` (tickets 2.2 / 2.3 / 2.4).
* Validating that a PATCH body scopes itself to the record's graph — the
  LDP layer parses and simulates the update before handing the
  *resulting* graph to :meth:`put_graph` (ticket 2.4).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rdflib import Graph, URIRef

from fdp.metadata.etag import compute_etag
from fdp.metadata.graphs import (
    audit_graph_uri,
    meta_graph_uri,
    record_graph_uri,
)
from fdp.metadata.meta import MetaResult, MetaWriter
from fdp.metadata.states import DEFAULT_STATE, MetadataState

if TYPE_CHECKING:
    from fdp.storage.triplestore import TripleStoreAdapter


class MetadataRepository:
    """Async CRUD over a single record's named graph plus its meta sibling."""

    def __init__(
        self,
        adapter: TripleStoreAdapter,
        *,
        meta_writer: MetaWriter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._meta = meta_writer or MetaWriter()
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- read ---------------------------------------------------------------

    async def get_graph(self, record_uri: str | URIRef) -> Graph:
        """Fetch the record's triples as an in-memory :class:`Graph`.

        Returns an empty graph if the record graph holds no triples — the
        caller (LDP layer) decides whether that is a 404 by also checking
        the meta graph for ``dct:created``.
        """
        graph_uri = record_graph_uri(record_uri)
        sparql = f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
        body = await self._adapter.query(sparql, accept="text/turtle")
        graph = Graph()
        if body:
            graph.parse(data=body.decode("utf-8"), format="turtle")
        return graph

    # --- write --------------------------------------------------------------

    async def put_graph(
        self,
        record_uri: str | URIRef,
        graph: Graph,
        *,
        subject: str | None,
        initial_state: MetadataState = DEFAULT_STATE,
    ) -> str:
        """Replace the record graph; return the post-write ETag.

        ``initial_state`` is the publication state for a *new* record
        (ADR-0010); ignored when the record already exists, since the meta
        builder preserves the prior state across content edits. The profile
        applier passes ``PUBLISHED`` for seeded records; the LDP layer leaves
        the ``DRAFT`` default.
        """
        graph_uri = record_graph_uri(record_uri)
        nt = graph.serialize(format="nt")
        await self._adapter.replace_graph(str(graph_uri), nt, mime="application/n-triples")
        await self._refresh_meta(record_uri, subject=subject, initial_state=initial_state)
        return compute_etag(graph)

    async def patch_graph(
        self,
        record_uri: str | URIRef,
        sparql_update: str,
        *,
        subject: str | None,
    ) -> str:
        """Execute ``sparql_update`` then refresh meta and return the new ETag.

        The update body is run verbatim against the triple store. The LDP
        layer no longer uses this path — its PATCH handler simulates
        locally and commits via :meth:`put_graph`. ``patch_graph`` stays
        as the lower-level escape hatch for the SPARQL endpoint (ticket
        3.x).
        """
        await self._adapter.update(sparql_update)
        await self._refresh_meta(record_uri, subject=subject)
        new_graph = await self.get_graph(record_uri)
        return compute_etag(new_graph)

    async def delete_graph(self, record_uri: str | URIRef) -> None:
        """Drop the record graph and both siblings.

        Right-to-erasure on individual Agreement entries within the audit
        graph is a separate admin endpoint concern; here we treat delete
        as a full record removal.
        """
        await self._adapter.drop_graph(str(record_graph_uri(record_uri)))
        await self._adapter.drop_graph(str(meta_graph_uri(record_uri)))
        await self._adapter.drop_graph(str(audit_graph_uri(record_uri)))

    # --- meta-metadata ------------------------------------------------------

    async def _refresh_meta(
        self,
        record_uri: str | URIRef,
        *,
        subject: str | None,
        initial_state: MetadataState = DEFAULT_STATE,
    ) -> MetaResult:
        prior = await self._get_meta(meta_graph_uri(record_uri))
        return await self._meta.write(
            self._adapter,
            record_iri=record_uri,
            prior=prior,
            subject=subject,
            now=self._clock(),
            initial_state=initial_state,
        )

    async def _get_meta(self, meta_uri: URIRef) -> Graph:
        sparql = f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{meta_uri}> {{ ?s ?p ?o }} }}"
        body = await self._adapter.query(sparql, accept="text/turtle")
        graph = Graph()
        if body:
            graph.parse(data=body.decode("utf-8"), format="turtle")
        return graph


__all__ = ["MetadataRepository"]
