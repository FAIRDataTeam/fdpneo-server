"""Named-graph URI conventions — the single source of truth.

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

Resource-definition records (ADR-0009) live under a reserved
``<base_url>/resource-definitions/<id>`` namespace and are *internal* —
FDP machinery rather than public knowledge graph.

These helpers live in the shared kernel because every bounded context
reasons about graph URIs: ``metadata`` does record CRUD, ``policy`` must
exclude internal graphs from the public dataset, ``access`` projects the
authorized set, and ``data`` scopes distribution queries. The helpers are
pure and are the only place the suffix / namespace conventions are encoded;
downstream code calls them rather than building URIs by hand.
"""

from __future__ import annotations

from rdflib import URIRef

from fdp.shared.reserved import RESERVED_API_PREFIX

_META_SUFFIX = "/meta"
_AUDIT_SUFFIX = "/audit"
_DATA_SUFFIX = "/data"

# Server-owned record namespaces all live under the single reserved API segment
# (see ``shared.reserved``) so the root namespace stays free for user-defined
# resource types. ``main.py`` mounts the matching routers under the same prefix.
#
# Resource-definition records (ADR-0009) are server-owned *internal* graphs.
# Policy and license documents (ADR-0012) are *public* reference documents
# (anonymous-readable, like ``/schemas``) — NOT internal, so
# ``is_internal_graph_uri`` does not exclude them; only their ``/meta`` and
# ``/audit`` siblings are internal, which the suffix predicates already cover.
_RESOURCE_DEFINITIONS_SEGMENT = f"{RESERVED_API_PREFIX}/resource-definitions"
_POLICIES_SEGMENT = f"{RESERVED_API_PREFIX}/policies"
_LICENSES_SEGMENT = f"{RESERVED_API_PREFIX}/licenses"
_SCHEMAS_SEGMENT = f"{RESERVED_API_PREFIX}/schemas"


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


def resource_definition_graph_uri(base_url: str | URIRef, rd_id: str) -> URIRef:
    """The graph URI holding the resource-definition record ``rd_id``.

    ``rd_id`` is a stable slug (typically the type's URL prefix, or a
    name-derived slug for the root). The IRI lives under the reserved
    resource-definitions namespace so it is recognised as internal.
    """
    base = str(base_url).rstrip("/")
    return URIRef(f"{base}/{_RESOURCE_DEFINITIONS_SEGMENT}/{rd_id}")


def policy_graph_uri(base_url: str | URIRef, policy_id: str) -> URIRef:
    """The stable, dereferenceable graph URI for managed policy ``policy_id`` (ADR-0012).

    Lives under the reserved ``<base_url>/policies/`` namespace. The Offer's
    own subject IRI equals this URI, so ``dct:rights`` references resolve here.
    """
    base = str(base_url).rstrip("/")
    return URIRef(f"{base}/{_POLICIES_SEGMENT}/{policy_id}")


def license_graph_uri(base_url: str | URIRef, license_id: str) -> URIRef:
    """The stable, dereferenceable graph URI for managed license ``license_id`` (ADR-0012).

    Lives under the reserved ``<base_url>/licenses/`` namespace; referenced
    descriptively via ``dct:license`` (never enforced by the PEP).
    """
    base = str(base_url).rstrip("/")
    return URIRef(f"{base}/{_LICENSES_SEGMENT}/{license_id}")


def schema_graph_uri(base_url: str | URIRef, schema_id: str) -> URIRef:
    """The stable, dereferenceable graph URI for managed SHACL shape ``schema_id``.

    Lives under the reserved ``<base_url>/fdp-api/schemas/`` namespace, served
    publicly (anonymous-readable) through the LDP/metadata layer.
    """
    base = str(base_url).rstrip("/")
    return URIRef(f"{base}/{_SCHEMAS_SEGMENT}/{schema_id}")


def policy_namespace(base_url: str | URIRef) -> str:
    """The ``<base>/fdp-api/policies`` prefix every managed-policy IRI starts with."""
    return f"{str(base_url).rstrip('/')}/{_POLICIES_SEGMENT}"


def license_namespace(base_url: str | URIRef) -> str:
    """The ``<base>/fdp-api/licenses`` prefix every managed-license IRI starts with."""
    return f"{str(base_url).rstrip('/')}/{_LICENSES_SEGMENT}"


def schema_namespace(base_url: str | URIRef) -> str:
    """The ``<base>/fdp-api/schemas`` prefix every managed-schema IRI starts with."""
    return f"{str(base_url).rstrip('/')}/{_SCHEMAS_SEGMENT}"


def is_policy_graph_uri(uri: str | URIRef) -> bool:
    """True iff ``uri`` is (or is a sibling of) a managed policy document."""
    return f"/{_POLICIES_SEGMENT}/" in str(uri)


def is_license_graph_uri(uri: str | URIRef) -> bool:
    """True iff ``uri`` is (or is a sibling of) a managed license document."""
    return f"/{_LICENSES_SEGMENT}/" in str(uri)


def is_meta_graph_uri(uri: str | URIRef) -> bool:
    return str(uri).endswith(_META_SUFFIX)


def is_schema_graph_uri(uri: str | URIRef) -> bool:
    """True iff ``uri`` is (or is a sibling of) a managed SHACL shape."""
    return f"/{_SCHEMAS_SEGMENT}/" in str(uri)


def is_audit_graph_uri(uri: str | URIRef) -> bool:
    return str(uri).endswith(_AUDIT_SUFFIX)


def is_data_graph_uri(uri: str | URIRef) -> bool:
    return str(uri).endswith(_DATA_SUFFIX)


def is_resource_definition_graph_uri(uri: str | URIRef) -> bool:
    """True iff ``uri`` is (or is a sibling of) a resource-definition record."""
    return f"/{_RESOURCE_DEFINITIONS_SEGMENT}/" in str(uri)


def is_internal_graph_uri(uri: str | URIRef) -> bool:
    """True iff ``uri`` is FDP machinery, not part of the public knowledge graph.

    The single, central definition of "internal" (ADR-0009): meta-metadata
    graphs, audit graphs, and resource-definition records. Both the SPARQL
    dataset projection (:meth:`fdp.policy.pdp.PDP.authorized_graphs`) and any
    future admin-visibility checks consult this one predicate, so there is
    exactly one place to get the exclusion right and one place to test it.

    Distribution *data* graphs (``…/data``) are deliberately **not** internal:
    they hold content served through the data provider's own scoped endpoint,
    not FDP machinery.
    """
    value = str(uri)
    return (
        is_meta_graph_uri(value)
        or is_audit_graph_uri(value)
        or is_resource_definition_graph_uri(value)
    )


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
    "is_internal_graph_uri",
    "is_license_graph_uri",
    "is_meta_graph_uri",
    "is_policy_graph_uri",
    "is_resource_definition_graph_uri",
    "is_schema_graph_uri",
    "license_graph_uri",
    "license_namespace",
    "meta_graph_uri",
    "policy_graph_uri",
    "policy_namespace",
    "record_graph_uri",
    "record_uri_from_sibling",
    "resource_definition_graph_uri",
    "schema_graph_uri",
    "schema_namespace",
]
