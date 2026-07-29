"""Per-record graph CRUD on top of the triple store adapter.

**Responsibilities**

* Get / put / patch / delete the record graph identified by a record URI.
* On every mutation, refresh the sibling meta graph via
  :class:`MetaWriter`. The meta builder (see :mod:`fdpneo_server.metadata.meta`)
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

from fdpneo_server.metadata.etag import compute_etag
from fdpneo_server.metadata.graphs import (
    audit_graph_uri,
    meta_graph_uri,
    record_graph_uri,
)
from fdpneo_server.metadata.meta import MetaResult, MetaWriter, build_meta_graph
from fdpneo_server.metadata.states import DEFAULT_STATE, MetadataState
from fdpneo_server.storage.triplestore.adapter import construct_named_graph

if TYPE_CHECKING:
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.storage.triplestore import TripleStoreAdapter


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

    def enable_meta_validation(self, *, validator: ShaclValidator, shape_iri: str) -> None:
        """Switch to a meta-writer that SHACL-validates the meta graph on write.

        Wired by app composition once the validator exists (the validator's
        shape provider reads through this repository, so the validating writer
        can only be installed after construction — avoiding a build cycle). A
        missing shape degrades safely; see :class:`fdpneo_server.metadata.meta.MetaWriter`.
        """
        self._meta = MetaWriter(validator=validator, shape_iri=shape_iri)

    # --- read ---------------------------------------------------------------

    async def get_graph(self, record_uri: str | URIRef) -> Graph:
        """Fetch the record's triples as an in-memory :class:`Graph`.

        Returns an empty graph if the record graph holds no triples — the
        caller (LDP layer) decides whether that is a 404 by also checking
        the meta graph for ``dct:created``.
        """
        return await construct_named_graph(self._adapter, str(record_graph_uri(record_uri)))

    async def get_meta(self, record_uri: str | URIRef) -> Graph:
        """Fetch the record's meta graph (provenance, version, state).

        Public read of the ``<record>/meta`` sibling — used by the search
        indexer to pull ``fdp:metadataState`` and ``dct:modified``.
        """
        return await construct_named_graph(self._adapter, str(meta_graph_uri(record_uri)))

    # --- write --------------------------------------------------------------

    async def put_graph(
        self,
        record_uri: str | URIRef,
        graph: Graph,
        *,
        subject: str | None,
        initial_state: MetadataState = DEFAULT_STATE,
        validated_against: str | None = None,
    ) -> str:
        """Replace the record graph; return the post-write ETag.

        ``initial_state`` is the publication state for a *new* record
        (ADR-0010); ignored when the record already exists, since the meta
        builder preserves the prior state across content edits. The profile
        applier passes ``PUBLISHED`` for seeded records; the LDP layer leaves
        the ``DRAFT`` default.

        ``validated_against`` (ADR-0019 §3) is the immutable profile version IRI
        the record content was validated against; stamped into the meta graph as
        ``fdp-o:validatedAgainst``. Omitted for writes that don't run record
        validation (the meta builder preserves any prior binding).
        """
        graph_uri = record_graph_uri(record_uri)
        nt = graph.serialize(format="nt")
        await self._adapter.replace_graph(str(graph_uri), nt, mime="application/n-triples")
        await self._refresh_meta(
            record_uri,
            subject=subject,
            initial_state=initial_state,
            validated_against=validated_against,
        )
        return compute_etag(graph)

    async def write_imported(
        self,
        record_uri: str | URIRef,
        graph: Graph,
        *,
        subject: str | None,
        created: datetime,
        modified: datetime,
        state: MetadataState = DEFAULT_STATE,
        validated_against: str | None = None,
    ) -> str:
        """Privileged provenance write for restore/import (ADR-0016 §5).

        Persists the record graph and a meta graph carrying **supplied** provenance
        — ``dct:created`` / ``dct:modified`` from the source, plus ``creator`` /
        ``state`` — instead of the server's ``now`` stamping. This capability is
        **CLI-only** (``fdp backup import``); it is never wired to an HTTP header or
        query flag, so the LDP contract's "server-stamped provenance always"
        guarantee (ADR-0014) stays un-gameable by API clients.
        """
        graph_uri = record_graph_uri(record_uri)
        await self._adapter.replace_graph(
            str(graph_uri), graph.serialize(format="nt"), mime="application/n-triples"
        )
        prior = await construct_named_graph(self._adapter, str(meta_graph_uri(record_uri)))
        result = build_meta_graph(
            record_iri=record_uri,
            prior=prior,
            subject=subject,
            now=self._clock(),
            initial_state=state,
            validated_against=validated_against,
            created=created,
            modified=modified,
        )
        await self._adapter.replace_graph(
            str(meta_graph_uri(record_uri)),
            result.graph.serialize(format="nt"),
            mime="application/n-triples",
        )
        return compute_etag(graph)

    async def delete_graph(self, record_uri: str | URIRef) -> None:
        """Drop the record graph and both siblings.

        Right-to-erasure on individual Agreement entries within the audit
        graph is a separate admin endpoint concern; here we treat delete
        as a full record removal.
        """
        await self._adapter.drop_graph(str(record_graph_uri(record_uri)))
        await self._adapter.drop_graph(str(meta_graph_uri(record_uri)))
        await self._adapter.drop_graph(str(audit_graph_uri(record_uri)))

    async def clear_all(self) -> None:
        """Wipe every graph in the store (force-apply / factory reset)."""
        await self._adapter.clear_all()

    # --- meta-metadata ------------------------------------------------------

    async def _refresh_meta(
        self,
        record_uri: str | URIRef,
        *,
        subject: str | None,
        initial_state: MetadataState = DEFAULT_STATE,
        validated_against: str | None = None,
    ) -> MetaResult:
        prior = await construct_named_graph(self._adapter, str(meta_graph_uri(record_uri)))
        return await self._meta.write(
            self._adapter,
            record_iri=record_uri,
            prior=prior,
            subject=subject,
            now=self._clock(),
            initial_state=initial_state,
            validated_against=validated_against,
        )


__all__ = ["MetadataRepository"]
