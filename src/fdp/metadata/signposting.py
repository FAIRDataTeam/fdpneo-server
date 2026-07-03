"""FAIR Signposting (Level 1) link builder (ADR-0017 §2).

Pure functions (no I/O), mirroring the discipline of :mod:`fdp.shared.identifiers`:
given a record's already-in-hand graph and its canonical IRI, produce the typed
``Link`` relations a machine agent uses to navigate and cite the record. The LDP
``GET``/``HEAD`` handlers (task 17.4) append :func:`render_link_header` output to
the response ``Link`` header; nothing here touches the store or the network.

The relations emitted (FAIR Signposting Profile, signposting.org/FAIR/):

* ``cite-as`` — the identifier a consumer should cite. Selection order
  (:func:`select_cite_as`) restores citation primacy to a client-supplied PID
  even though the record is served at its own canonical IRI.
* ``describedby`` — the record's alternate RDF serializations (the canonical IRI
  once per supported media type, carrying a ``type`` attribute).
* ``type`` — each ``rdf:type`` of the canonical subject.
* ``license`` — IRI-valued ``dct:license``.
* ``author`` — IRI-valued ``dct:creator`` / ``dct:publisher``.
* ``item`` — a container's members (``ldp:contains`` + its typed member
  relations), downward.
* ``collection`` — ``dct:isPartOf``, upward.

Level 2 (a ``linkset`` document for link sets too large for headers) is deferred
(ADR-0017 §2); the per-response link count is capped at :data:`MAX_LINKS`, and
trimming ``item`` links first is acceptable Level-1 degradation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.shared.graphs import record_graph_uri
from fdp.shared.namespaces import ADMS, DCT, LDP, OWL, SKOS

# Hosts whose IRIs are recognised persistent-identifier resolvers, plus the
# ``ark:`` scheme (handled separately). Used to decide whether a client-supplied
# identifier deserves ``cite-as`` primacy over the canonical IRI.
PID_RESOLVERS: frozenset[str] = frozenset(
    {"doi.org", "dx.doi.org", "hdl.handle.net", "w3id.org", "purl.org", "identifiers.org"}
)

# Cap on the total signposting links emitted per response, so a huge container
# cannot blow up the header block (ADR-0017 §2). Fixed relations are kept; only
# surplus ``item`` links are trimmed.
MAX_LINKS = 30


@dataclass(frozen=True)
class Link:
    """One RFC 8288 web link: a target IRI, a relation, and an optional type."""

    target: str
    rel: str
    type: str | None = None


def is_pid_iri(value: str) -> bool:
    """Whether ``value`` is a persistent identifier under a recognised resolver.

    True for an ``ark:`` scheme IRI or an IRI whose host is in
    :data:`PID_RESOLVERS`; false otherwise (including the FDP's own canonical
    IRIs, which are not third-party PIDs).
    """
    candidate = value.strip()
    if candidate.startswith("ark:"):
        return True
    host = urlsplit(candidate).netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    return host in PID_RESOLVERS


def select_cite_as(record_graph: Graph, canonical_iri: str) -> str:
    """The IRI a consumer should cite for the record (ADR-0017 §2).

    Order: (1) a client-asserted ``owl:sameAs`` under a recognised PID resolver,
    (2) an ``adms:identifier`` notation or IRI-valued ``dct:identifier`` under a
    recognised PID resolver, else (3) the canonical IRI. Ties within a tier break
    lexicographically, so the choice is deterministic.
    """
    subject = record_graph_uri(canonical_iri)

    tier1 = sorted(
        str(o)
        for o in record_graph.objects(subject, OWL.sameAs)
        if isinstance(o, URIRef) and is_pid_iri(str(o))
    )
    if tier1:
        return tier1[0]

    tier2: list[str] = []
    for node in record_graph.objects(subject, ADMS.identifier):
        tier2.extend(
            str(notation)
            for notation in record_graph.objects(node, SKOS.notation)
            if is_pid_iri(str(notation))
        )
    tier2.extend(
        str(o) for o in record_graph.objects(subject, DCT.identifier) if is_pid_iri(str(o))
    )
    if tier2:
        return sorted(tier2)[0]

    return str(subject)


def signposting_links(
    record_graph: Graph, canonical_iri: str, media_types: Sequence[str]
) -> list[Link]:
    """Build the FAIR Signposting Level-1 links for a record.

    ``media_types`` are the supported RDF serializations (pass
    :data:`fdp.shared.negotiation.SUPPORTED_TYPES`); one ``describedby`` link is
    emitted per type. Fixed relations come first; ``item`` links fill the
    remaining budget up to :data:`MAX_LINKS`.
    """
    subject = record_graph_uri(canonical_iri)
    canonical = str(subject)

    links: list[Link] = [Link(select_cite_as(record_graph, canonical_iri), "cite-as")]
    links.extend(Link(canonical, "describedby", type=mt) for mt in media_types)
    links.extend(Link(t, "type") for t in _iri_objects(record_graph, subject, RDF.type))
    links.extend(Link(lic, "license") for lic in _iri_objects(record_graph, subject, DCT.license))
    authors = sorted(
        set(_iri_objects(record_graph, subject, DCT.creator))
        | set(_iri_objects(record_graph, subject, DCT.publisher))
    )
    links.extend(Link(a, "author") for a in authors)
    links.extend(Link(c, "collection") for c in _iri_objects(record_graph, subject, DCT.isPartOf))

    # item: container members — ldp:contains plus each typed member relation the
    # Direct Container config declares (ldp:hasMemberRelation → dcat:dataset …).
    items = set(_iri_objects(record_graph, subject, LDP.contains))
    for predicate in record_graph.objects(subject, LDP.hasMemberRelation):
        if isinstance(predicate, URIRef):
            items.update(_iri_objects(record_graph, subject, predicate))
    budget = max(MAX_LINKS - len(links), 0)
    links.extend(Link(i, "item") for i in sorted(items)[:budget])
    return links


def render_link_header(links: Iterable[Link]) -> str:
    """Serialize ``links`` to an RFC 8288 ``Link`` header value (comma-joined)."""
    return ", ".join(_render_link(link) for link in links)


def _render_link(link: Link) -> str:
    parts = [f"<{link.target}>", f'rel="{link.rel}"']
    if link.type is not None:
        parts.append(f'type="{link.type}"')
    return "; ".join(parts)


def _iri_objects(graph: Graph, subject: URIRef, predicate: URIRef) -> list[str]:
    """Sorted, de-duplicated IRI objects of ``(subject, predicate, ?)``."""
    return sorted({str(o) for o in graph.objects(subject, predicate) if isinstance(o, URIRef)})


__all__ = [
    "MAX_LINKS",
    "PID_RESOLVERS",
    "Link",
    "is_pid_iri",
    "render_link_header",
    "select_cite_as",
    "signposting_links",
]
