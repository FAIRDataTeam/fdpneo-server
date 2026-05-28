"""Runtime :class:`ShapeProvider` backed by the metadata repository.

Architecture §5.3 stores SHACL schemas as records — every shape lives
at its own named graph in the triple store, written by
:mod:`fdp.metadata.profiles.applier`. This provider hands those graphs
to :class:`fdp.metadata.shacl.ShaclValidator` so the LDP router can
validate writes against the shape declared by the resource's type
without needing a separate shape store.

The fetch returns Turtle (what :class:`ShaclValidator` parses).
An empty graph at the requested IRI — typically because the profile
didn't declare that schema or wasn't applied yet — surfaces as
:class:`UnknownShapeError`, which the validator wraps into its
fall-through behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fdp.metadata.shacl import UnknownShapeError

if TYPE_CHECKING:
    from fdp.metadata.repository import MetadataRepository


class MetadataShapeProvider:
    """Resolves shape IRIs by reading record graphs from the triple store.

    Stateless; safe to share across requests. Caching of the parsed
    shape graph is the validator's job — this provider just reaches
    into the metadata repository and serialises whatever it finds.
    """

    def __init__(self, repository: MetadataRepository) -> None:
        self._repository = repository

    async def fetch(self, shape_iri: str) -> str:
        graph = await self._repository.get_graph(shape_iri)
        if len(graph) == 0:
            raise UnknownShapeError(shape_iri)
        data = graph.serialize(format="turtle")
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return data


__all__ = ["MetadataShapeProvider"]
