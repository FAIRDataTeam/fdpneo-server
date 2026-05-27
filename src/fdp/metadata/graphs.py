"""Per-record graph URI conventions.

Every metadata record is one named graph identified by the record's URI
(ADR-0007). Two siblings accompany it:

* meta graph at ``<record-uri>/meta`` — provenance / version state.
* audit graph at ``<record-uri>/audit`` — materialized ODRL Agreements.

A *data* graph at ``<record-uri>/data`` is a sibling specific to
distribution records — it holds the distribution's actual triples,
separate from the metadata describing the distribution. The data graph
lives outside ADR-0007's one-graph-per-record scope because it is
content, not metadata; the data provider scopes per-distribution SPARQL
queries to it.

These helpers are pure. They are the only place the suffix convention is
encoded; downstream code calls them rather than building URIs by hand.
"""

from __future__ import annotations

from rdflib import URIRef

_META_SUFFIX = "/meta"
_AUDIT_SUFFIX = "/audit"
_DATA_SUFFIX = "/data"


def _as_uri(record_uri: str | URIRef) -> URIRef:
    if isinstance(record_uri, URIRef):
        return record_uri
    return URIRef(record_uri)


def _stripped(record_uri: str | URIRef) -> str:
    """Return the record URI without any trailing slash."""
    value = str(_as_uri(record_uri))
    return value.rstrip("/")


def record_graph_uri(record_uri: str | URIRef) -> URIRef:
    """The graph URI that holds the record's own triples.

    Equal to the record's URI itself (with any trailing slash removed so
    the meta / audit siblings are unambiguous).
    """
    return URIRef(_stripped(record_uri))


def meta_graph_uri(record_uri: str | URIRef) -> URIRef:
    """The sibling graph holding meta-metadata for the record."""
    return URIRef(_stripped(record_uri) + _META_SUFFIX)


def audit_graph_uri(record_uri: str | URIRef) -> URIRef:
    """The sibling graph holding materialized Agreements for the record."""
    return URIRef(_stripped(record_uri) + _AUDIT_SUFFIX)


def data_graph_uri(record_uri: str | URIRef) -> URIRef:
    """The graph holding a distribution's actual data triples.

    Only meaningful for distribution records; querying this graph for a
    non-distribution returns nothing.
    """
    return URIRef(_stripped(record_uri) + _DATA_SUFFIX)


def is_meta_graph_uri(uri: str | URIRef) -> bool:
    return str(uri).endswith(_META_SUFFIX)


def is_audit_graph_uri(uri: str | URIRef) -> bool:
    return str(uri).endswith(_AUDIT_SUFFIX)


def is_data_graph_uri(uri: str | URIRef) -> bool:
    return str(uri).endswith(_DATA_SUFFIX)


def record_uri_from_sibling(uri: str | URIRef) -> URIRef | None:
    """If ``uri`` is a meta / audit / data sibling, return the record URI; else ``None``."""
    value = str(uri)
    for suffix in (_META_SUFFIX, _AUDIT_SUFFIX, _DATA_SUFFIX):
        if value.endswith(suffix):
            return URIRef(value[: -len(suffix)])
    return None


__all__ = [
    "audit_graph_uri",
    "data_graph_uri",
    "is_audit_graph_uri",
    "is_data_graph_uri",
    "is_meta_graph_uri",
    "meta_graph_uri",
    "record_graph_uri",
    "record_uri_from_sibling",
]
