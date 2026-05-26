"""LDP server — exposes records under the W3C Linked Data Platform REST API.

Public surface:

* :func:`build_ldp_router` — factory that wires :class:`MetadataRepository`,
  :class:`PDP`, an optional :class:`ShaclValidator` and an optional
  :class:`ContainerRegistry` into a FastAPI router.

See :mod:`fdp.metadata.ldp.router` for routing semantics and
:mod:`fdp.metadata.ldp.negotiation` for content negotiation.
"""

from fdp.metadata.ldp.router import (
    ContainerRegistry,
    DefaultContainerRegistry,
    build_ldp_router,
)

__all__ = [
    "ContainerRegistry",
    "DefaultContainerRegistry",
    "build_ldp_router",
]
