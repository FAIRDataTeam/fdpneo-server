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
OWL = Namespace("http://www.w3.org/2002/07/owl#")
PROV = Namespace("http://www.w3.org/ns/prov#")
SH = Namespace("http://www.w3.org/ns/shacl#")
XSD = Namespace("http://www.w3.org/2001/XMLSchema#")

FDP_DEFAULT = Namespace("https://w3id.org/fdp/o#")
"""Fallback ``fdp:`` namespace when no settings override is configured."""

# --- FDP ontology terms ----------------------------------------------------
#
# These are *fixed* vocabulary terms in the published FDP ontology, NOT
# deployment-configurable. A deployment may rebrand the namespace it mints
# record/schema IRIs in (``fdp_namespace``), but the ontology terms the FDP
# itself stamps onto its own machinery records — resource definitions and
# their child links (ADR-009) — always live at ``https://w3id.org/fdp/o#``,
# the same way ``META_SHAPE_IRI`` does for meta-metadata. Keeping them here
# makes the registry the single source of truth for the vocabulary.

FDP_RESOURCE_DEFINITION = FDP_DEFAULT["ResourceDefinition"]
"""``rdf:type`` of a resource-definition record."""

FDP_URL_PREFIX = FDP_DEFAULT["urlPrefix"]
"""Route segment a resource definition is exposed under (``""`` for root)."""

FDP_NAME = FDP_DEFAULT["name"]
"""Type name — drives OpenAPI tags and operation ids."""

FDP_CHILD_LINK = FDP_DEFAULT["childLink"]
"""Links a resource definition to a child-link node."""

FDP_RELATION_URI = FDP_DEFAULT["relationUri"]
"""Predicate a parent uses to point at members of the target type."""

FDP_CHILD_TARGET = FDP_DEFAULT["childTarget"]
"""``urlPrefix`` of the target resource definition of a child link."""

FDP_CHILD_TITLE = FDP_DEFAULT["childTitle"]
"""Human label rendered in the child-listing endpoint."""

FDP_CHILD_TAGS_URI = FDP_DEFAULT["childTagsUri"]
"""Optional predicate naming the tag vocabulary for a child listing."""

FDP_METADATA_STATE = FDP_DEFAULT["metadataState"]
"""Publication-state predicate stamped on a record's meta graph (ADR-0010).

Object is one of the literal values ``DRAFT`` / ``PUBLISHED`` / ``ARCHIVED``.
Lives in ``<record>/meta`` alongside ``dct:modified`` and ``owl:versionInfo``;
changed only through the ``POST /{record}/state`` transition API, never by a
record-content edit."""

PREFIXES: Mapping[str, Namespace] = MappingProxyType(
    {
        "dcat": DCAT,
        "dct": DCT,
        "foaf": FOAF,
        "ldp": LDP,
        "odrl": ODRL,
        "owl": OWL,
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
    "FDP_CHILD_LINK",
    "FDP_CHILD_TAGS_URI",
    "FDP_CHILD_TARGET",
    "FDP_CHILD_TITLE",
    "FDP_DEFAULT",
    "FDP_NAME",
    "FDP_RELATION_URI",
    "FDP_RESOURCE_DEFINITION",
    "FDP_URL_PREFIX",
    "FOAF",
    "LDP",
    "ODRL",
    "OWL",
    "PREFIXES",
    "PROV",
    "SH",
    "XSD",
    "bind_all",
    "fdp_namespace",
]
