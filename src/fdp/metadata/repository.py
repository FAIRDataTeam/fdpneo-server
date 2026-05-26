"""Per-record graph CRUD on top of the triple store adapter.

**Responsibilities**

* Get / put / patch / delete the record graph identified by a record URI.
* On every mutation, refresh the sibling meta graph: bump
  ``owl:versionInfo``, update ``dct:modified``, stamp ``dct:created`` and
  ``dct:creator`` on the first write.
* Compute the post-write ETag of the record graph so the LDP layer can
  return it in ``ETag`` headers for ``If-Match`` concurrency control.

**Non-responsibilities**

* SHACL validation — ticket 2.2 + 2.5.
* Validating that a PATCH body scopes itself to the record's graph — the
  LDP layer parses and validates the update before handing the string
  here; this module just executes it.
* PROV ``Activity`` blank nodes — minimal meta-metadata only for now;
  ticket 2.5 owns the full schema.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.etag import compute_etag
from fdp.metadata.graphs import (
    audit_graph_uri,
    meta_graph_uri,
    record_graph_uri,
)
from fdp.shared.namespaces import DCT, OWL, PROV

if TYPE_CHECKING:
    from fdp.storage.triplestore import TripleStoreAdapter


class MetadataRepository:
    """Async CRUD over a single record's named graph plus its meta sibling."""

    def __init__(
        self,
        adapter: TripleStoreAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
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
        creator: str | None,
    ) -> str:
        """Replace the record graph; return the post-write ETag."""
        graph_uri = record_graph_uri(record_uri)
        nt = graph.serialize(format="nt")
        await self._adapter.replace_graph(str(graph_uri), nt, mime="application/n-triples")
        await self._refresh_meta(record_uri, creator=creator)
        return compute_etag(graph)

    async def patch_graph(
        self,
        record_uri: str | URIRef,
        sparql_update: str,
        *,
        creator: str | None,
    ) -> str:
        """Execute ``sparql_update`` then refresh meta and return the new ETag.

        The update body is run verbatim — the LDP layer is responsible
        for verifying it only targets the record's graph.
        """
        await self._adapter.update(sparql_update)
        await self._refresh_meta(record_uri, creator=creator)
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
        creator: str | None,
    ) -> None:
        """Read current meta, bump version, write the new meta graph."""
        meta_uri = meta_graph_uri(record_uri)
        record_subject = record_graph_uri(record_uri)
        now = self._clock()

        prior = await self._get_meta(meta_uri)
        version = self._next_version(prior, record_subject)
        created_at, prior_creator = self._extract_creation(prior, record_subject)

        next_graph = Graph()
        next_graph.add((record_subject, RDF.type, PROV.Entity))

        effective_created = created_at or now
        effective_creator = prior_creator or creator

        next_graph.add((record_subject, DCT.created, Literal(effective_created.isoformat())))
        if effective_creator is not None:
            next_graph.add((record_subject, DCT.creator, URIRef(effective_creator)))
        next_graph.add((record_subject, DCT.modified, Literal(now.isoformat())))
        next_graph.add((record_subject, OWL.versionInfo, Literal(version)))

        nt = next_graph.serialize(format="nt")
        await self._adapter.replace_graph(str(meta_uri), nt, mime="application/n-triples")

    async def _get_meta(self, meta_uri: URIRef) -> Graph:
        sparql = f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{meta_uri}> {{ ?s ?p ?o }} }}"
        body = await self._adapter.query(sparql, accept="text/turtle")
        graph = Graph()
        if body:
            graph.parse(data=body.decode("utf-8"), format="turtle")
        return graph

    @staticmethod
    def _next_version(prior: Graph, record_subject: URIRef) -> int:
        for value in prior.objects(record_subject, OWL.versionInfo):
            if isinstance(value, Literal):
                try:
                    return int(str(value)) + 1
                except ValueError:
                    return 1
        return 1

    @staticmethod
    def _extract_creation(
        prior: Graph,
        record_subject: URIRef,
    ) -> tuple[datetime | None, str | None]:
        created: datetime | None = None
        for value in prior.objects(record_subject, DCT.created):
            if isinstance(value, Literal):
                try:
                    created = datetime.fromisoformat(str(value))
                except ValueError:
                    created = None
            break
        creator: str | None = None
        for value in prior.objects(record_subject, DCT.creator):
            if isinstance(value, URIRef):
                creator = str(value)
                break
        return created, creator


__all__ = ["MetadataRepository"]
