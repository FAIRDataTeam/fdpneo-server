"""Per-record graph URI conventions.

The conventions themselves now live in :mod:`fdp.shared.graphs` so every
bounded context (``policy`` excluding internal graphs from the public
dataset, ``access`` projecting the authorized set, ``data`` scoping
distribution queries) can use them without importing ``metadata`` and
crossing a module boundary. This module re-exports them unchanged so the
many ``from fdp.metadata.graphs import …`` call sites keep working.
"""

from __future__ import annotations

from fdp.shared.graphs import (
    audit_graph_uri,
    data_graph_uri,
    is_audit_graph_uri,
    is_data_graph_uri,
    is_internal_graph_uri,
    is_meta_graph_uri,
    is_resource_definition_graph_uri,
    meta_graph_uri,
    record_graph_uri,
    record_uri_from_sibling,
    resource_definition_graph_uri,
    state_record_iri,
)

__all__ = [
    "audit_graph_uri",
    "data_graph_uri",
    "is_audit_graph_uri",
    "is_data_graph_uri",
    "is_internal_graph_uri",
    "is_meta_graph_uri",
    "is_resource_definition_graph_uri",
    "meta_graph_uri",
    "record_graph_uri",
    "record_uri_from_sibling",
    "resource_definition_graph_uri",
    "state_record_iri",
]
