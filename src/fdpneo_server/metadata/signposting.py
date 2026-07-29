"""FAIR Signposting (Level 1) link builder (ADR-0017 §2).

Pure functions (no I/O), mirroring the discipline of :mod:`fdpneo_server.shared.identifiers`:
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

from fdpneo_server.shared.graphs import record_graph_uri
from fdpneo_server.shared.namespaces import ADMS, DCT, LDP, OWL, SKOS

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

# FDP-O extension relation types for in-band affordance advertisement
# (ADR-0022 §2). These always live at the published ontology IRI
# ``https://w3id.org/fdp/o#`` — never the deployment-rebranded ``fdp:`` namespace
# — so a harvester recognises them regardless of local branding. They are opaque
# extension rels (RFC 8288 §2.1.2); if the FDP-O WG standardizes equivalents a
# later ADR swaps the IRIs, a compatible substitution for conforming clients.
_FDP_O = "https://w3id.org/fdp/o#"
REL_HAS_META_METADATA = _FDP_O + "hasMetaMetadata"
REL_HAS_SPEC = _FDP_O + "hasSpec"
REL_HAS_EXPANDED_VIEW = _FDP_O + "hasExpandedView"
REL_HAS_STATE_TRANSITION = _FDP_O + "hasStateTransition"
REL_HAS_MEMBER_PAGE = _FDP_O + "hasMemberPage"
REL_HAS_RESOURCE_DEFINITIONS = _FDP_O + "hasResourceDefinitions"


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
    record_graph: Graph,
    canonical_iri: str,
    media_types: Sequence[str],
    *,
    reserved: int = 0,
) -> list[Link]:
    """Build the FAIR Signposting Level-1 links for a record.

    ``media_types`` are the supported RDF serializations (pass
    :data:`fdpneo_server.shared.negotiation.SUPPORTED_TYPES`); one ``describedby`` link is
    emitted per type. Fixed relations come first; ``item`` links fill the
    remaining budget up to :data:`MAX_LINKS`.

    ``reserved`` is the number of additional *fixed* links the caller will append
    after these (the ADR-0022 affordance links). It is subtracted from the
    ``item`` budget so the combined set still honours :data:`MAX_LINKS` while
    those fixed relations always survive — only surplus ``item`` links are trimmed.
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
    budget = max(MAX_LINKS - len(links) - reserved, 0)
    links.extend(Link(i, "item") for i in sorted(items)[:budget])
    return links


def affordance_links(
    canonical_iri: str,
    *,
    is_container: bool,
    url_prefix: str | None,
    child_prefixes: Sequence[str],
    base_url: str | None,
) -> list[Link]:
    """Build the ADR-0022 §2 in-band affordance links for a record GET/HEAD.

    These advertise the management views a client would otherwise reach only by
    URL-template convention: the ``/meta`` sibling graph, the instance- and
    type-level ``/spec`` SHACL views, ``/expanded``, the ``/state`` transition
    endpoint, and, for a container, one member-page link per declared child type.

    Links are emitted **unconditionally** — hypermedia advertises the affordance;
    the PDP still gates the request (a ``/state`` link on a record the caller
    cannot transition is normal, the endpoint answers 401/403). All are *fixed*
    relations for the :data:`MAX_LINKS` trim policy.

    A record of a type with no resource definition (``url_prefix is None`` — e.g.
    an internal/managed document) exposes only ``/meta``; the other views are
    resource-definition-driven and do not apply. The type-level ``/spec`` link is
    emitted only when ``base_url`` is known.
    """
    record = canonical_iri.rstrip("/")
    links = [Link(f"{record}/meta", REL_HAS_META_METADATA)]
    if url_prefix is None:
        return links

    links.append(Link(f"{record}/spec", REL_HAS_SPEC))
    if base_url is not None:
        base = base_url.rstrip("/")
        type_spec = f"{base}/spec" if url_prefix == "" else f"{base}/{url_prefix}/spec"
        # For the root record the instance and type views coincide; avoid a dupe.
        if type_spec != f"{record}/spec":
            links.append(Link(type_spec, REL_HAS_SPEC))
    links.append(Link(f"{record}/expanded", REL_HAS_EXPANDED_VIEW))
    links.append(Link(f"{record}/state", REL_HAS_STATE_TRANSITION))
    if is_container:
        links.extend(
            Link(f"{record}/page/{child}", REL_HAS_MEMBER_PAGE) for child in child_prefixes
        )
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
    "REL_HAS_EXPANDED_VIEW",
    "REL_HAS_MEMBER_PAGE",
    "REL_HAS_META_METADATA",
    "REL_HAS_RESOURCE_DEFINITIONS",
    "REL_HAS_SPEC",
    "REL_HAS_STATE_TRANSITION",
    "Link",
    "affordance_links",
    "is_pid_iri",
    "render_link_header",
    "select_cite_as",
    "signposting_links",
]
