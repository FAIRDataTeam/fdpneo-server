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

    from fdpneo_server.config import Settings


ADMS = Namespace("http://www.w3.org/ns/adms#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
LDP = Namespace("http://www.w3.org/ns/ldp#")
ODRL = Namespace("http://www.w3.org/ns/odrl/2/")
OWL = Namespace("http://www.w3.org/2002/07/owl#")
PROF = Namespace("http://www.w3.org/ns/dx/prof/")
PROV = Namespace("http://www.w3.org/ns/prov#")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
# The W3C profile-role registry (prof:hasRole values, e.g. role:validation).
ROLE = Namespace("http://www.w3.org/ns/dx/prof/role/")
SDO = Namespace("https://schema.org/")
SH = Namespace("http://www.w3.org/ns/shacl#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
SPDX = Namespace("http://spdx.org/rdf/terms#")
VOID = Namespace("http://rdfs.org/ns/void#")
XSD = Namespace("http://www.w3.org/2001/XMLSchema#")

FDP_DEFAULT = Namespace("https://w3id.org/fdp/fdp-o#")
"""Fallback ``fdp:`` namespace when no settings override is configured."""

FDP_LEGACY = Namespace("https://w3id.org/fdp/o#")
"""The pre-0.16 namespace — a typo for the published FDP Ontology IRI.

``https://w3id.org/fdp/o#`` was never registered on w3id.org (it 404s); the
FDP Ontology lives at ``https://w3id.org/fdp/fdp-o#``. Kept ONLY so the
startup vocabulary migration (ADR-0026) can recognise and rewrite stored
data minted by older releases. Never mint new terms here."""

# --- FDP ontology terms ----------------------------------------------------
#
# These are *fixed* vocabulary terms, NOT deployment-configurable. A
# deployment may rebrand the namespace it mints record/schema IRIs in
# (``fdp_namespace``), but the terms the FDP itself stamps — the published
# FDP-O classes/predicates plus FDPneo's lifecycle and resource-definition
# machinery terms (proposed for FDP-O inclusion, see
# docs/proposals/fdp-o-additions.md) — always live at the FDP Ontology IRI
# ``https://w3id.org/fdp/fdp-o#``. Keeping them here makes the registry the
# single source of truth for the vocabulary.

FDP_FAIRDATAPOINT = FDP_DEFAULT["FAIRDataPoint"]
"""The FDP root class (published FDP-O term)."""

FDP_METADATA_SERVICE = FDP_DEFAULT["MetadataService"]
"""Superclass of ``fdp-o:FAIRDataPoint`` (published FDP-O term).

Asserted alongside ``FAIRDataPoint`` on the root record: FDP Index
validators (e.g. home.fairdatapoint.org) match ``MetadataService``
literally, with no subclass inference."""

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

FDP_VALIDATED_AGAINST = FDP_DEFAULT["validatedAgainst"]
"""Meta-graph predicate recording the exact profile *version* a record was
validated against at write time (ADR-0019 §3). Object is the immutable profile
version IRI (``…/fdp-api/profiles/<slug>/<version>``); lives in ``<record>/meta``
alongside ``owl:versionInfo``, and travels in the dump so a restore reproduces
the original validation. The record graph itself carries the *stable* profile via
``dct:conformsTo`` — current binding in the record, exact version in the meta."""

FDP_METADATA_STATE = FDP_DEFAULT["metadataState"]
"""Publication-state predicate stamped on a record's meta graph (ADR-0010).

Object is one of the literal values ``DRAFT`` / ``PUBLISHED`` / ``ARCHIVED``.
Lives in ``<record>/meta`` alongside ``dct:modified`` and ``owl:versionInfo``;
changed only through the ``POST /{record}/state`` transition API, never by a
record-content edit."""

FDP_ALLOWED_STATE_TRANSITION = FDP_DEFAULT["allowedStateTransition"]
"""Read-time *view* predicate on a served ``<record>/meta`` representation
(ADR-0022 §3). One literal object (``DRAFT`` / ``PUBLISHED`` / ``ARCHIVED``) per
state the record may transition to next, computed from the lifecycle state
machine when the meta graph is served. **Never persisted** — it is not stored,
not returned by ``fdp dump``, and not seen by the meta-graph SHACL validator;
it advertises the ``POST /{record}/state`` affordance so a client learns what a
record may become without consulting OpenAPI."""

PREFIXES: Mapping[str, Namespace] = MappingProxyType(
    {
        "adms": ADMS,
        "dcat": DCAT,
        "dct": DCT,
        "foaf": FOAF,
        # The FDP ontology terms (fdp-o:Metadata, fdp-o:MetadataService,
        # fdp-o:FAIRDataPoint, fdp-o:servesMetadata …) are FIXED — they live at
        # the published ontology IRI, not the deployment-rebranded `fdp:`
        # namespace — so this prefix maps to FDP_DEFAULT, unlike the `fdp:`
        # special-case in IRIExpander.
        "fdp-o": FDP_DEFAULT,
        "ldp": LDP,
        "odrl": ODRL,
        "owl": OWL,
        "prof": PROF,
        "prov": PROV,
        "rdfs": RDFS,
        "role": ROLE,
        "sdo": SDO,
        "sh": SH,
        "skos": SKOS,
        "spdx": SPDX,
        "void": VOID,
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
        from fdpneo_server.config import get_settings

        settings = get_settings()
    return Namespace(str(settings.fdp_namespace))


def bind_all(graph: Graph, *, settings: Settings | None = None) -> None:
    """Bind every standard prefix plus the deployment's ``fdp:`` to ``graph``."""
    for prefix, namespace in PREFIXES.items():
        graph.bind(prefix, namespace, override=True)
    graph.bind("fdp", fdp_namespace(settings), override=True)


__all__ = [
    "ADMS",
    "DCAT",
    "DCT",
    "FDP_ALLOWED_STATE_TRANSITION",
    "FDP_CHILD_LINK",
    "FDP_CHILD_TAGS_URI",
    "FDP_CHILD_TARGET",
    "FDP_CHILD_TITLE",
    "FDP_DEFAULT",
    "FDP_FAIRDATAPOINT",
    "FDP_LEGACY",
    "FDP_METADATA_SERVICE",
    "FDP_NAME",
    "FDP_RELATION_URI",
    "FDP_RESOURCE_DEFINITION",
    "FDP_URL_PREFIX",
    "FDP_VALIDATED_AGAINST",
    "FOAF",
    "LDP",
    "ODRL",
    "OWL",
    "PREFIXES",
    "PROF",
    "PROV",
    "RDFS",
    "ROLE",
    "SDO",
    "SH",
    "SKOS",
    "SPDX",
    "VOID",
    "XSD",
    "bind_all",
    "fdp_namespace",
]
