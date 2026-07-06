"""``fdp dump`` - storage-level export (ADR-0016 section 2).

Reads **every named graph** in the triple store through the adapter (records +
their ``/meta`` / ``/audit`` siblings, plus the reserved profile / schema /
resource-definition / policy / license graphs and their immutable version
snapshots - all captured because they are named graphs) and writes them verbatim
as N-Quads, alongside a versioned ``manifest.json`` and an optional ``audit.jsonl``
of the Postgres ``record_audit`` rows. Nothing is interpreted on the way out - the
named-graph keying *is* the record identity (ADR-0007).

Blank-node labels are re-minted per graph with globally-unique ids so that,
because N-Quads scopes blank nodes to the whole document, two records' blank
nodes can never be conflated on restore. This is faithful: FDP named graphs are
independent (a blank node never spans graphs), so within-graph identity is
preserved and cross-graph identity was never asserted.

Admin-operated CLI tooling: synchronous filesystem writes are fine here - nothing
else runs on the loop during a dump.
"""
# ruff: noqa: ASYNC240 - CLI-only admin tooling; blocking file I/O is intentional.

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from rdflib import BNode, Graph
from rdflib.term import Node
from sqlalchemy import select

from fdp import __version__
from fdp.metadata.audit import RecordAuditRow
from fdp.shared.graphs import is_profile_graph_uri
from fdp.storage.triplestore.adapter import SPARQL_JSON, construct_named_graph

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

DUMP_FORMAT_VERSION = "1"
"""Bumped only on a breaking change to the on-disk archive layout."""

# The data-model version records whether the ADR-0019 record/schema binding is
# present, so `restore` can migrate a pre-binding dump forward (task 18.3).
DATA_MODEL_ADR0019 = "adr-0019"
DATA_MODEL_LEGACY = "legacy"

RECORDS_FILE = "records.nq"
MANIFEST_FILE = "manifest.json"
AUDIT_FILE = "audit.jsonl"


@dataclass
class DumpResult:
    """Outcome of a dump, for the CLI to report."""

    out_dir: Path
    graph_count: int
    quad_count: int
    audit_rows: int
    data_model_version: str
    files: dict[str, str]  # filename -> sha256 hex


async def dump_store(
    adapter: TripleStoreAdapter,
    out_dir: Path | str,
    *,
    identifier_base: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    include_audit: bool = True,
) -> DumpResult:
    """Dump every named graph to ``out_dir`` as an ADR-0016 archive.

    ``session_factory`` enables the optional ``audit.jsonl`` export of the Postgres
    ``record_audit`` rows; omit it (or ``include_audit=False``) to dump the triple
    store only.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    graph_uris = await _all_graph_uris(adapter)
    records_path = out / RECORDS_FILE
    hasher = hashlib.sha256()
    quad_count = 0
    written_graphs = 0
    has_profiles = False

    with records_path.open("wb") as handle:
        for graph_uri in graph_uris:
            if is_profile_graph_uri(graph_uri):
                has_profiles = True
            graph = await construct_named_graph(adapter, graph_uri)
            if len(graph) == 0:
                continue
            block = _graph_to_nquads(graph_uri, graph).encode("utf-8")
            handle.write(block)
            hasher.update(block)
            quad_count += len(graph)
            written_graphs += 1

    files = {RECORDS_FILE: hasher.hexdigest()}

    audit_rows = 0
    if include_audit and session_factory is not None:
        audit_rows, audit_hash = await _dump_audit(session_factory, out / AUDIT_FILE)
        files[AUDIT_FILE] = audit_hash

    data_model = DATA_MODEL_ADR0019 if has_profiles else DATA_MODEL_LEGACY
    manifest = {
        "dump_format_version": DUMP_FORMAT_VERSION,
        "identifier_base": identifier_base,
        "application_version": __version__,
        "data_model_version": data_model,
        "graph_count": written_graphs,
        "quad_count": quad_count,
        "audit_rows": audit_rows,
        "created_at": datetime.now(UTC).isoformat(),
        "files": files,
    }
    (out / MANIFEST_FILE).write_bytes(
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    )
    log.info(
        "dump_complete",
        out_dir=str(out),
        graphs=written_graphs,
        quads=quad_count,
        audit_rows=audit_rows,
        data_model=data_model,
    )
    return DumpResult(
        out_dir=out,
        graph_count=written_graphs,
        quad_count=quad_count,
        audit_rows=audit_rows,
        data_model_version=data_model,
        files=files,
    )


async def _all_graph_uris(adapter: TripleStoreAdapter) -> list[str]:
    """Every named graph that holds at least one triple, sorted for determinism."""
    body = await adapter.query(
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", accept=SPARQL_JSON
    )
    bindings = json.loads(body).get("results", {}).get("bindings", [])
    return sorted(b["g"]["value"] for b in bindings if "g" in b)


def _graph_to_nquads(graph_uri: str, graph: Graph) -> str:
    """Serialize one named graph as N-Quads, re-minting blank nodes uniquely.

    N-Triples + a graph-column rewrite, rather than rdflib's deprecation-noisy
    ``Dataset``: relabel each blank node to a fresh globally-unique id, serialize
    to N-Triples, then append the graph IRI as the fourth term of every statement.
    """
    relabeled = Graph()
    remap: dict[BNode, BNode] = {}

    def _term(term: Node) -> Node:
        if isinstance(term, BNode):
            return remap.setdefault(term, BNode())  # fresh, globally-unique label
        return term

    for subject, predicate, obj in graph:
        relabeled.add((_term(subject), _term(predicate), _term(obj)))

    suffix = f" <{graph_uri}> ."
    out_lines = [
        f"{stripped.removesuffix(' .')}{suffix}\n"
        for line in relabeled.serialize(format="nt").splitlines()
        if (stripped := line.strip())
    ]
    return "".join(out_lines)


async def _dump_audit(
    session_factory: async_sessionmaker[AsyncSession], path: Path
) -> tuple[int, str]:
    """Write the ``record_audit`` rows to ``path`` as JSON Lines; return (count, sha256)."""
    hasher = hashlib.sha256()
    count = 0
    with path.open("wb") as handle:
        async with session_factory() as session:
            result = await session.stream_scalars(
                select(RecordAuditRow).order_by(RecordAuditRow.id)
            )
            async for row in result:
                line = (
                    json.dumps(
                        {
                            "id": row.id,
                            "record_iri": row.record_iri,
                            "operation": row.operation,
                            "subject": row.subject,
                            "etag": row.etag,
                            "occurred_at": row.occurred_at.isoformat(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                handle.write(line)
                hasher.update(line)
                count += 1
    return count, hasher.hexdigest()


__all__ = [
    "AUDIT_FILE",
    "DATA_MODEL_ADR0019",
    "DATA_MODEL_LEGACY",
    "DUMP_FORMAT_VERSION",
    "MANIFEST_FILE",
    "RECORDS_FILE",
    "DumpResult",
    "dump_store",
]
