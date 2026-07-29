"""Runtime :class:`ShapeProvider` backed by the metadata repository.

Architecture §5.3 stores SHACL schemas as records — every shape lives
at its own named graph in the triple store, written by
:mod:`fdpneo_server.metadata.profiles.applier`. This provider hands those graphs
to :class:`fdpneo_server.metadata.shacl.ShaclValidator` so the LDP router can
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

from fdpneo_server.metadata.shacl import UnknownShapeError

if TYPE_CHECKING:
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.shacl import ShapeProvider


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


class PredefinedShapeProvider:
    """Serves fixed, server-owned SHACL shapes from code; delegates the rest.

    Some shapes are *server-owned constants* (e.g. the managed-license shape,
    ADR-0012), not profile artifacts. Resolving them must not depend on whether
    or when a profile was applied — otherwise a deployment whose profile was
    applied before a new server shape existed never seeds it, and every
    validation against it 500s with :class:`UnknownShapeError`. This provider
    answers those IRIs from the in-code definitions and falls back to the
    triple-store-backed ``delegate`` for profile-declared schema shapes.

    Stateless; safe to share across requests.
    """

    def __init__(self, *, predefined: dict[str, str], delegate: ShapeProvider) -> None:
        self._predefined = dict(predefined)
        self._delegate = delegate

    async def fetch(self, shape_iri: str) -> str:
        predefined = self._predefined.get(shape_iri)
        if predefined is not None:
            return predefined
        return await self._delegate.fetch(shape_iri)


__all__ = ["MetadataShapeProvider", "PredefinedShapeProvider"]
