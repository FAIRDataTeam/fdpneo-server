"""One-time identifier-base adoption migration (v0.3.0, ADR-0014).

A deployment bootstrapped before persistent identifiers minted every record,
schema, policy, license and resource-definition graph under ``base_url``. When it
later adopts a persistent ``identifier_base`` (e.g. a W3ID prefix), the existing
IRIs must move to the new base — once. After adoption the identifier base never
changes again (a deployment move only re-points the redirector), so this is not a
recurring operation.

The rewrite is non-destructive in spirit and idempotent: each named graph under
the old base is re-keyed under the new base and every IRI term inside it (subject,
predicate, or object) that sits under the old base is rewritten too — so
cross-record links (``dct:isPartOf``, ``dcat:dataset``, ``ldp:membershipResource``,
…) keep resolving. A second pass finds nothing under the old base and is a no-op.

Server-owned graph URIs are interpolated into the enumerating/reading SPARQL, the
same controlled-input pattern the repository uses (``repository.py``); no
untrusted value is ever interpolated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import structlog
from rdflib import Graph, URIRef

from fdp.storage.triplestore.adapter import TripleStoreAdapter, construct_named_graph

__all__ = ["RebaseReport", "rebase_identifiers"]

log = structlog.get_logger(__name__)


@dataclass
class RebaseReport:
    """Summary of a rebase run."""

    old_base: str
    new_base: str
    dry_run: bool
    moved: list[tuple[str, str]] = field(default_factory=list)
    """(old graph URI, new graph URI) pairs that were (or would be) moved."""

    @property
    def count(self) -> int:
        return len(self.moved)


def _rebased(value: str, old: str, new: str) -> str | None:
    """Return ``value`` re-rooted from ``old`` to ``new``, or None if unaffected."""
    if value == old:
        return new
    for sep in ("/", "#", "?"):
        if value.startswith(old + sep):
            return new + value[len(old) :]
    return None


def _rewrite_term(term: object, old: str, new: str) -> object:
    if isinstance(term, URIRef):
        moved = _rebased(str(term), old, new)
        if moved is not None:
            return URIRef(moved)
    return term


def _rewrite_graph(graph: Graph, old: str, new: str) -> Graph:
    out = Graph()
    for s, p, o in graph:
        out.add(
            (
                _rewrite_term(s, old, new),  # type: ignore[arg-type]
                _rewrite_term(p, old, new),  # type: ignore[arg-type]
                _rewrite_term(o, old, new),  # type: ignore[arg-type]
            )
        )
    return out


async def _list_graphs(adapter: TripleStoreAdapter) -> list[str]:
    body = await adapter.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    payload = json.loads(body)
    bindings = payload.get("results", {}).get("bindings", [])
    return [b["g"]["value"] for b in bindings if "g" in b]


async def rebase_identifiers(
    *,
    adapter: TripleStoreAdapter,
    old_base: str,
    new_base: str,
    dry_run: bool = False,
) -> RebaseReport:
    """Move every graph under ``old_base`` to ``new_base``, rewriting IRIs.

    Args:
        adapter: The triple-store adapter.
        old_base: The base records currently live under (the old ``base_url``).
        new_base: The persistent identifier base to adopt.
        dry_run: When True, enumerate and report without writing.

    Returns:
        A :class:`RebaseReport`. Idempotent: re-running after a successful pass
        moves nothing.
    """
    old = old_base.rstrip("/")
    new = new_base.rstrip("/")
    report = RebaseReport(old_base=old, new_base=new, dry_run=dry_run)
    if old == new:
        log.info("pid_rebase_noop_same_base", base=old)
        return report

    for graph_uri in await _list_graphs(adapter):
        new_uri = _rebased(graph_uri, old, new)
        if new_uri is None:
            continue  # already under the new base, or unrelated/internal
        report.moved.append((graph_uri, new_uri))
        if dry_run:
            continue
        rewritten = _rewrite_graph(await construct_named_graph(adapter, graph_uri), old, new)
        await adapter.replace_graph(
            new_uri, rewritten.serialize(format="nt"), mime="application/n-triples"
        )
        await adapter.drop_graph(graph_uri)

    log.info(
        "pid_rebase_completed",
        old_base=old,
        new_base=new,
        moved=report.count,
        dry_run=dry_run,
    )
    return report
