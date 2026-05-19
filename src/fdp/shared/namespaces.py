"""RDF namespace registry.

**Responsibilities**

* Hold the canonical ``rdflib.Namespace`` for every vocabulary the server
  uses. Every other module imports prefixes from here; redefining them
  elsewhere is a smell.
* Resolve the deployment's configurable ``fdp:`` namespace from ``Settings``.

**Non-responsibilities**

* RDF helpers (canonicalization for ETags, graph diffing, format conversion).
  Those live in their own module under ``shared`` when they are added.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from rdflib import Graph, Namespace

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fdp.config import Settings


DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
LDP = Namespace("http://www.w3.org/ns/ldp#")
ODRL = Namespace("http://www.w3.org/ns/odrl/2/")
PROV = Namespace("http://www.w3.org/ns/prov#")
SH = Namespace("http://www.w3.org/ns/shacl#")
XSD = Namespace("http://www.w3.org/2001/XMLSchema#")

FDP_DEFAULT = Namespace("https://w3id.org/fdp/o#")
"""Fallback ``fdp:`` namespace when no settings override is configured."""

PREFIXES: Mapping[str, Namespace] = MappingProxyType(
    {
        "dcat": DCAT,
        "dct": DCT,
        "foaf": FOAF,
        "ldp": LDP,
        "odrl": ODRL,
        "prov": PROV,
        "sh": SH,
        "xsd": XSD,
    }
)


def fdp_namespace(settings: Settings | None = None) -> Namespace:
    """Return the configured ``fdp:`` namespace for this deployment.

    If ``settings`` is omitted, the global ``get_settings()`` cache is read.
    Tests construct a ``Settings`` instance and pass it explicitly to avoid
    relying on environment state.
    """
    if settings is None:
        from fdp.config import get_settings

        settings = get_settings()
    return Namespace(str(settings.fdp_namespace))


def bind_all(graph: Graph, *, settings: Settings | None = None) -> None:
    """Bind every standard prefix plus the deployment's ``fdp:`` to ``graph``."""
    for prefix, namespace in PREFIXES.items():
        graph.bind(prefix, namespace, override=True)
    graph.bind("fdp", fdp_namespace(settings), override=True)


__all__ = [
    "DCAT",
    "DCT",
    "FDP_DEFAULT",
    "FOAF",
    "LDP",
    "ODRL",
    "PREFIXES",
    "PROV",
    "SH",
    "XSD",
    "bind_all",
    "fdp_namespace",
]
