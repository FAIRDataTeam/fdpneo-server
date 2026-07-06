"""``fdp backup restore`` - faithful, verbatim import (ADR-0016 section 3).

Loads a dump's quads back through the adapter with **no** ``MetaWriter`` stamping,
so ``dct:created`` / ``dct:modified`` / creator / state, the ADR-0019 binding
(``dct:conformsTo`` / ``fdp-o:validatedAgainst``) and the ``/audit`` graphs survive
exactly. Faithful restore is the only mode; re-publication (fresh timestamps) is
what the LDP API is for.

Preconditions (ADR-0016 section 3):

* the target ``identifier_base`` must equal the manifest's - on mismatch the
  command refuses and points at ``fdp backup import --rebase`` (identifier
  persistence means a restore never needs new IRIs);
* a non-empty store is refused unless ``--merge`` (skip existing graphs) or
  ``--overwrite`` (replace them) is given.

Parsing is byte-level string work (split each N-Quads line into its N-Triple body
+ graph IRI), so triples - including their exact blank-node labels - are handed to
the store verbatim, and rdflib's deprecation-noisy ``Dataset`` is avoided.

Admin-operated CLI tooling: synchronous filesystem reads are fine here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from rdflib import Graph

from fdp.metadata.audit import RecordAuditRow
from fdp.metadata.backup.dump import (
    AUDIT_FILE,
    DATA_MODEL_LEGACY,
    MANIFEST_FILE,
    RECORDS_FILE,
)
from fdp.metadata.pid.rebase import rebased, rewrite_graph
from fdp.storage.triplestore.adapter import SPARQL_JSON

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from fdp.storage.triplestore import TripleStoreAdapter

log = structlog.get_logger(__name__)

_NT = "application/n-triples"
# One N-Quads statement: `<ntriple body> <graph-iri> .`. The graph is always an
# IRI in an FDP dump, so the last `<...>` before the final `.` is the graph and
# everything before it is a valid N-Triple. Greedy `.*` leaves the graph as the
# final angle-bracketed term (an IRI object earlier in the line stays in the body).
_QUAD = re.compile(r"^(?P<triple>.*) <(?P<graph>[^>]+)> \.$")


class RestoreError(Exception):
    """A restore precondition failed; the CLI maps it to a clear message + exit 1."""


@dataclass
class RestoreResult:
    """Outcome of a restore, for the CLI to report and to drive follow-up steps."""

    graphs_loaded: int
    graphs_skipped: int
    quad_count: int
    data_model_version: str
    identifier_base: str
    needs_migration: bool
    dry_run: bool


async def restore_store(
    adapter: TripleStoreAdapter,
    in_dir: Path | str,
    *,
    target_identifier_base: str,
    merge: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
    rebase: bool = False,
) -> RestoreResult:
    """Load a dump into the triple store, enforcing ADR-0016 preconditions.

    ``rebase`` (task 18.4) adopts a dump captured under a *different* base: every
    graph IRI and every IRI term under the dump's ``identifier_base`` is re-rooted
    to ``target_identifier_base`` in flight (including the ADR-0019 cross-references
    ``dct:conformsTo`` / ``fdp-o:validatedAgainst`` / ``prof:hasArtifact`` and the
    profile / schema graphs, since those live under the same base). Without
    ``rebase`` a base mismatch is refused - a faithful restore never re-mints IRIs.
    """
    src = Path(in_dir)
    manifest = json.loads((src / MANIFEST_FILE).read_text(encoding="utf-8"))

    records_path = src / RECORDS_FILE
    _verify_checksum(records_path, manifest.get("files", {}).get(RECORDS_FILE))

    manifest_base = str(manifest.get("identifier_base", "")).rstrip("/")
    target_base = target_identifier_base.rstrip("/")
    if manifest_base != target_base and not rebase:
        raise RestoreError(
            f"identifier_base mismatch: dump is {manifest_base!r} but this deployment is "
            f"{target_base!r}. A faithful restore never re-mints IRIs — use "
            f"`fdp backup import --rebase` to adopt a dump under a different base."
        )

    existing = set(await _all_graph_uris(adapter))
    if existing and not (merge or overwrite) and not dry_run:
        raise RestoreError(
            f"target store is not empty ({len(existing)} graph(s)). Pass --merge to skip "
            f"existing graphs or --overwrite to replace them."
        )

    groups = _parse_nquads_by_graph(records_path.read_text(encoding="utf-8"))
    loaded = 0
    skipped = 0
    quads = 0
    for graph_uri, triples in groups.items():
        dest_uri = graph_uri
        payload = "".join(triples)
        if rebase:
            dest_uri = rebased(graph_uri, manifest_base, target_base) or graph_uri
            payload = _rebase_payload(payload, manifest_base, target_base)
        if merge and dest_uri in existing:
            skipped += 1
            continue
        quads += len(triples)
        if not dry_run:
            await adapter.replace_graph(dest_uri, payload, mime=_NT)
        loaded += 1

    data_model = str(manifest.get("data_model_version", DATA_MODEL_LEGACY))
    result = RestoreResult(
        graphs_loaded=loaded,
        graphs_skipped=skipped,
        quad_count=quads,
        data_model_version=data_model,
        identifier_base=target_base if rebase else manifest_base,
        needs_migration=data_model == DATA_MODEL_LEGACY,
        dry_run=dry_run,
    )
    log.info(
        "restore_complete",
        loaded=loaded,
        skipped=skipped,
        quads=quads,
        data_model=data_model,
        dry_run=dry_run,
    )
    return result


async def restore_audit(
    session_factory: async_sessionmaker[AsyncSession], in_dir: Path | str
) -> int:
    """Insert the dump's ``audit.jsonl`` rows into ``record_audit``; return the count.

    A no-op when the file is absent. Row ids are left to autoincrement (they are
    history, not live references) so an append into an existing table cannot clash.
    """
    path = Path(in_dir) / AUDIT_FILE
    if not path.exists():
        return 0
    rows = [
        RecordAuditRow(
            record_iri=entry["record_iri"],
            operation=entry["operation"],
            subject=entry.get("subject"),
            etag=entry.get("etag"),
            occurred_at=datetime.fromisoformat(entry["occurred_at"]),
        )
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for entry in (json.loads(line),)
    ]
    if not rows:
        return 0
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
    return len(rows)


def _rebase_payload(nt_text: str, old: str, new: str) -> str:
    """Re-root every IRI under ``old`` to ``new`` in a graph's N-Triples payload."""
    graph = Graph()
    graph.parse(data=nt_text, format="nt")
    return rewrite_graph(graph, old, new).serialize(format="nt")


def _parse_nquads_by_graph(text: str) -> dict[str, list[str]]:
    """Group N-Quads lines by graph IRI, returning graph → list of N-Triple lines."""
    groups: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _QUAD.match(line)
        if match is None:
            raise RestoreError(f"malformed N-Quads line in {RECORDS_FILE}: {line[:80]!r}")
        groups.setdefault(match["graph"], []).append(f"{match['triple']} .\n")
    return groups


def _verify_checksum(path: Path, expected: str | None) -> None:
    if not path.exists():
        raise RestoreError(f"missing {path.name} in the dump directory")
    if not expected:
        return  # manifest without a checksum for this file — nothing to verify
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RestoreError(
            f"{path.name} checksum mismatch (dump corrupt or modified): "
            f"expected {expected[:12]}…, got {actual[:12]}…"
        )


async def _all_graph_uris(adapter: TripleStoreAdapter) -> list[str]:
    body = await adapter.query(
        "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", accept=SPARQL_JSON
    )
    bindings = json.loads(body).get("results", {}).get("bindings", [])
    return [b["g"]["value"] for b in bindings if "g" in b]


__all__ = ["RestoreError", "RestoreResult", "restore_audit", "restore_store"]
