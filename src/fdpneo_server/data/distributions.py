"""Distribution lookup and metadata extraction (architecture §5.6).

Pulls a distribution's record graph through :class:`MetadataRepository`
and parses out the three properties the data provider needs:

* ``dcat:downloadURL`` — where the file content lives (used by the file
  endpoint to redirect or proxy).
* ``dcat:accessURL`` — its presence flags the distribution as having an
  RDF endpoint; the data provider hosts that endpoint scoped to the
  distribution's data graph.
* ``dct:rights`` — the Offer URI consulted by the policy module to
  decide whether anonymous read is permitted.

The resolver does not call the policy module itself; the router owns
the authorization step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from rdflib import URIRef

from fdpneo_server.shared.errors import NotFound
from fdpneo_server.shared.namespaces import DCAT, DCT

if TYPE_CHECKING:
    from rdflib import Graph


class RecordReader(Protocol):
    """Anything that can fetch a record graph by IRI.

    Declared as a local protocol so the data module does not import
    :mod:`fdpneo_server.metadata.repository` directly (CLAUDE.md layering rule:
    ``data/`` may import only shared, policy, storage).
    :class:`fdpneo_server.metadata.repository.MetadataRepository` satisfies this
    by duck typing.
    """

    async def get_graph(self, record_uri: str) -> Graph: ...


@dataclass(frozen=True)
class DistributionInfo:
    """The slice of a distribution's metadata the data provider needs."""

    iri: str
    download_url: str | None
    access_url: str | None
    rights_iri: str | None

    @property
    def has_download(self) -> bool:
        return self.download_url is not None

    @property
    def has_access(self) -> bool:
        return self.access_url is not None


async def resolve_distribution(
    distribution_iri: str,
    *,
    repository: RecordReader,
) -> DistributionInfo:
    """Fetch and parse the distribution metadata at ``distribution_iri``.

    Raises :class:`NotFound` if the record graph is empty — the LDP layer
    would treat that the same way, and the data provider has no separate
    notion of "record exists but has no data".
    """
    graph = await repository.get_graph(distribution_iri)
    if len(graph) == 0:
        raise NotFound(
            f"no distribution at {distribution_iri}",
            details={"iri": distribution_iri},
        )

    subject = URIRef(distribution_iri)
    download = _single_object(graph, subject, DCAT.downloadURL)
    access = _single_object(graph, subject, DCAT.accessURL)
    rights = _single_object(graph, subject, DCT.rights)
    return DistributionInfo(
        iri=distribution_iri,
        download_url=download,
        access_url=access,
        rights_iri=rights,
    )


def _single_object(graph: object, subject: URIRef, predicate: URIRef) -> str | None:
    """Return the lexical value of ``(subject, predicate, ?o)`` or ``None``.

    If multiple values are present, the first wins — distributions
    declaring more than one ``downloadURL`` are out of scope for v1.
    """
    # rdflib.Graph.objects(...) returns an iterator of nodes.
    for obj in graph.objects(subject, predicate):  # type: ignore[union-attr]
        return str(obj)
    return None


__all__ = ["DistributionInfo", "RecordReader", "resolve_distribution"]
