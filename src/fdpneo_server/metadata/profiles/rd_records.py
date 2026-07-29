"""Resource-definition records: the RDF representation + its predefined shape.

A *resource definition* (RD) describes one metadata type the deployment
exposes — its URL prefix, the SHACL shape that validates its instances, and
the typed child links that place it in the hierarchy. Under ADR-009 RDs are
stored as ordinary RDF records (one named graph each, under the reserved
``…/resource-definitions/`` namespace) and are runtime-mutable, rather than
being derived once from the profile manifest.

This module owns the *record* layer — the on-the-wire RDF form and the
predefined SHACL shape that validates it. It deliberately does **not** know
about storage (the reserved IRI namespace, the applier, the triple store —
those belong to the storage task) or about the resolved
:class:`~fdpneo_server.metadata.profiles.registry.ResourceDefinitionCache` (which
cross-references child targets to other definitions). The split mirrors the
existing manifest→cache layering:

* manifest ``ResourceDefinitionEntry`` (CURIEs)  → bootstrap seed
* **``ResourceDefinitionRecord`` (expanded IRIs) → this module ↔ RDF**
* ``ResourceDefinition`` (resolved children)     → the runtime cache

``ResourceDefinitionRecord`` is the *unresolved* form: a child link carries
only the target's ``urlPrefix``; resolving that to the target type's name and
schema IRI is the cache builder's job (it needs every record in hand to do
the cross-reference), exactly as ``build_cache_from_manifest`` does today.

The vocabulary terms live in :mod:`fdpneo_server.shared.namespaces` (fixed FDP ontology
terms, not deployment-configurable). The shape's own IRI is
:data:`RD_SHAPE_IRI`, fixed the same way as
:data:`fdpneo_server.metadata.meta.META_SHAPE_IRI`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from fdpneo_server.shared.namespaces import (
    FDP_CHILD_LINK,
    FDP_CHILD_TAGS_URI,
    FDP_CHILD_TARGET,
    FDP_CHILD_TITLE,
    FDP_NAME,
    FDP_RELATION_URI,
    FDP_RESOURCE_DEFINITION,
    FDP_URL_PREFIX,
    LDP,
    bind_all,
)

# Fixed IRI of the predefined shape, like META_SHAPE_IRI. Not deployment-
# configurable — see the FDP-ontology-terms note in shared.namespaces.
RD_SHAPE_IRI = "https://w3id.org/fdp/o#ResourceDefinitionShape"


# --- record dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class ChildLinkRecord:
    """An unresolved child link: predicate + the target's url prefix.

    ``relation_uri`` and ``tags_uri`` are absolute IRIs; ``target_prefix`` is
    the route segment of the target definition (resolved to a name/schema by
    the cache builder, not here).
    """

    relation_uri: str
    target_prefix: str
    title: str = ""
    tags_uri: str | None = None


@dataclass(frozen=True)
class ResourceDefinitionRecord:
    """One resource definition in its stored, unresolved form (expanded IRIs)."""

    url_prefix: str
    name: str
    schema_iri: str
    children: tuple[ChildLinkRecord, ...] = ()

    @property
    def is_root(self) -> bool:
        return self.url_prefix == ""


def rd_record_slug(url_prefix: str, name: str) -> str:
    """Stable id segment for a resource-definition record's IRI.

    The URL prefix is the natural id and matches the route; the root
    (empty prefix) falls back to a slug of its name (``Repository`` →
    ``repository``). Shared by the bootstrap applier and the runtime admin
    service so both mint the same IRI for a given definition.
    """
    if url_prefix:
        return url_prefix
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


# --- RDF (de)serialization -------------------------------------------------


class ResourceDefinitionParseError(ValueError):
    """A graph at an RD IRI is missing a required term or is malformed.

    Writes are guarded by the predefined SHACL shape, so this should only
    surface on hand-crafted or corrupted data; it is raised rather than
    silently coerced so the cache builder fails loudly.
    """


def record_to_graph(record: ResourceDefinitionRecord, iri: str) -> Graph:
    """Serialize ``record`` to the RDF graph stored at ``iri``.

    Child links are blank nodes hung off ``fdp:childLink``; their order is
    not significant (RDF is a set), so callers must not depend on it.
    """
    subject = URIRef(iri)
    graph = Graph()
    bind_all(graph)
    graph.add((subject, RDF.type, FDP_RESOURCE_DEFINITION))
    graph.add((subject, FDP_URL_PREFIX, Literal(record.url_prefix, datatype=XSD.string)))
    graph.add((subject, FDP_NAME, Literal(record.name, datatype=XSD.string)))
    graph.add((subject, LDP.constrainedBy, URIRef(record.schema_iri)))
    for child in record.children:
        node = BNode()
        graph.add((subject, FDP_CHILD_LINK, node))
        graph.add((node, FDP_RELATION_URI, URIRef(child.relation_uri)))
        graph.add((node, FDP_CHILD_TARGET, Literal(child.target_prefix, datatype=XSD.string)))
        if child.title:
            graph.add((node, FDP_CHILD_TITLE, Literal(child.title, datatype=XSD.string)))
        if child.tags_uri is not None:
            graph.add((node, FDP_CHILD_TAGS_URI, URIRef(child.tags_uri)))
    return graph


def record_from_graph(graph: Graph, iri: str) -> ResourceDefinitionRecord:
    """Parse the RD record rooted at ``iri`` out of ``graph``.

    Raises :class:`ResourceDefinitionParseError` if a required term
    (``fdp:urlPrefix``, ``fdp:name``, ``ldp:constrainedBy``) is absent.
    """
    subject = URIRef(iri)
    url_prefix = _one_literal(graph, subject, FDP_URL_PREFIX, iri, "fdp:urlPrefix")
    name = _one_literal(graph, subject, FDP_NAME, iri, "fdp:name")
    schema_iri = _one_iri(graph, subject, LDP.constrainedBy, iri, "ldp:constrainedBy")

    children: list[ChildLinkRecord] = []
    for node in graph.objects(subject, FDP_CHILD_LINK):
        if not isinstance(node, (URIRef, BNode)):
            # A literal hung off fdp:childLink is malformed; the predefined
            # shape rejects it on write, so ignore it rather than crash here.
            continue
        relation = _one_iri(graph, node, FDP_RELATION_URI, iri, "fdp:relationUri")
        target = _one_literal(graph, node, FDP_CHILD_TARGET, iri, "fdp:childTarget")
        title_obj = graph.value(node, FDP_CHILD_TITLE)
        tags_obj = graph.value(node, FDP_CHILD_TAGS_URI)
        children.append(
            ChildLinkRecord(
                relation_uri=relation,
                target_prefix=target,
                title=str(title_obj) if title_obj is not None else "",
                tags_uri=str(tags_obj) if tags_obj is not None else None,
            )
        )
    # Sort children deterministically — RDF stores blank nodes unordered, and
    # callers (cache builder, OpenAPI generator) benefit from a stable order.
    children.sort(key=lambda c: (c.relation_uri, c.target_prefix))
    return ResourceDefinitionRecord(
        url_prefix=url_prefix,
        name=name,
        schema_iri=schema_iri,
        children=tuple(children),
    )


@lru_cache(maxsize=1)
def predefined_shape_graph() -> Graph:
    """The predefined SHACL shape that validates RD records.

    Fixed and server-owned (not a profile artifact, unlike the DCAT shapes),
    so it lives in code as the single source of truth and is written to the
    store at its fixed IRI by the storage/bootstrap task. Cached because it is
    immutable.
    """
    graph = Graph()
    graph.parse(data=_SHAPE_TTL, format="turtle")
    return graph


# --- internals -------------------------------------------------------------


def _one_literal(graph: Graph, subject: URIRef | BNode, pred: URIRef, iri: str, label: str) -> str:
    value = graph.value(subject, pred)
    if value is None:
        raise ResourceDefinitionParseError(f"{iri}: missing required {label}")
    return str(value)


def _one_iri(graph: Graph, subject: URIRef | BNode, pred: URIRef, iri: str, label: str) -> str:
    value = graph.value(subject, pred)
    if not isinstance(value, URIRef):
        raise ResourceDefinitionParseError(f"{iri}: {label} must be present and an IRI")
    return str(value)


_SHAPE_TTL = """\
@prefix sh:  <http://www.w3.org/ns/shacl#> .
@prefix ldp: <http://www.w3.org/ns/ldp#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix fdp: <https://w3id.org/fdp/o#> .

fdp:ResourceDefinitionShape
    a sh:NodeShape ;
    sh:targetClass fdp:ResourceDefinition ;
    sh:property [
        sh:path fdp:urlPrefix ;
        sh:name "url prefix" ;
        sh:description "Route segment; the empty string denotes the root Repository." ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:datatype xsd:string ;
    ] ;
    sh:property [
        sh:path fdp:name ;
        sh:name "name" ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:datatype xsd:string ;
    ] ;
    sh:property [
        sh:path ldp:constrainedBy ;
        sh:name "shape" ;
        sh:description "SHACL shape that validates instances of this type." ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:nodeKind sh:IRI ;
    ] ;
    sh:property [
        sh:path fdp:childLink ;
        sh:name "child link" ;
        sh:description "Typed link to a child resource definition." ;
        sh:nodeKind sh:BlankNodeOrIRI ;
        sh:node fdp:ChildLinkShape ;
    ] .

fdp:ChildLinkShape
    a sh:NodeShape ;
    sh:property [
        sh:path fdp:relationUri ;
        sh:description "Predicate the parent uses to point at members of the target type." ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:nodeKind sh:IRI ;
    ] ;
    sh:property [
        sh:path fdp:childTarget ;
        sh:description "urlPrefix of the target resource definition." ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:datatype xsd:string ;
    ] ;
    sh:property [
        sh:path fdp:childTitle ;
        sh:maxCount 1 ;
        sh:datatype xsd:string ;
    ] ;
    sh:property [
        sh:path fdp:childTagsUri ;
        sh:maxCount 1 ;
        sh:nodeKind sh:IRI ;
    ] .
"""


__all__ = [
    "RD_SHAPE_IRI",
    "ChildLinkRecord",
    "ResourceDefinitionParseError",
    "ResourceDefinitionRecord",
    "predefined_shape_graph",
    "rd_record_slug",
    "record_from_graph",
    "record_to_graph",
]
