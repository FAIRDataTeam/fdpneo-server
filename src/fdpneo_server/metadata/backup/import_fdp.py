"""``fdp backup import --from <url>`` — migrate from a reference-FDP instance.

Walks a source FDP's LDP tree over HTTP (content-negotiated ``GET``, following
``ldp:contains`` breadth-first), and for each record (ADR-0016 §4):

* **re-roots** every host-bound source IRI to this deployment's ``identifier_base``
  + the same path (records and their cross-links), via the shared ``pid/rebase``
  rewrite — so the migrated record's identity is under our persistent base;
* **carries provenance** — the source's ``dct:issued`` / ``dct:modified`` become
  the meta graph's ``dct:created`` / ``dct:modified`` (privileged write path, 18.6),
  not the import time;
* **preserves the old IRI** as a structured alternative identifier
  (``adms:identifier`` + ``dct:identifier``, ADR-0017), never ``owl:sameAs``;
* **validates as a report** — when the (re-rooted) record carries a
  ``dct:conformsTo`` a validator can resolve, violations are collected into the
  report, never a hard reject (ADR-0016 §3 posture).

Egress is pinned to the source origin: only IRIs under ``source_base`` are fetched
(no dereferencing of arbitrary IRIs found in the metadata). Binding the imported
records to *this* deployment's profiles (``dct:conformsTo`` / ``validatedAgainst``)
is done by the caller afterwards via the conformance backfill.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS

from fdpneo_server.metadata.identifiers import record_alternative_identifier
from fdpneo_server.metadata.pid.rebase import rebased, rewrite_graph
from fdpneo_server.metadata.states import MetadataState
from fdpneo_server.shared.namespaces import LDP

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fdpneo_server.metadata.repository import MetadataRepository

log = structlog.get_logger(__name__)

_RDF_ACCEPT = "text/turtle, application/ld+json;q=0.9, application/rdf+xml;q=0.8"
# Provenance predicates on the source, most-specific first.
_ISSUED = (DCTERMS.issued, DCTERMS.created)
_MODIFIED = (DCTERMS.modified,)


@dataclass
class ImportReport:
    """Outcome of a reference-FDP import."""

    source_base: str
    target_base: str
    dry_run: bool
    imported: list[tuple[str, str]] = field(default_factory=list)  # (old IRI, new IRI)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (IRI, reason)
    validation_issues: list[tuple[str, str]] = field(default_factory=list)  # (new IRI, issue)
    truncated: bool = False

    @property
    def count(self) -> int:
        return len(self.imported)


async def import_reference_fdp(
    *,
    repository: MetadataRepository,
    http_client: httpx.AsyncClient,
    source_base: str,
    target_base: str,
    dry_run: bool = False,
    max_records: int = 10_000,
    validate: Callable[[str, Graph], Awaitable[list[str]]] | None = None,
) -> ImportReport:
    """Crawl a reference FDP from ``source_base`` and import its records.

    ``repository`` writes via the privileged provenance path
    (:meth:`MetadataRepository.write_imported`). ``validate`` is an optional
    report-only hook run per imported record.
    """
    source = source_base.rstrip("/")
    target = target_base.rstrip("/")
    report = ImportReport(source_base=source, target_base=target, dry_run=dry_run)

    queue: deque[str] = deque([source])
    visited: set[str] = set()
    while queue:
        if report.count >= max_records:
            report.truncated = True
            break
        iri = queue.popleft()
        if iri in visited:
            continue
        visited.add(iri)

        graph = await _fetch(http_client, iri, source)
        if graph is None:
            report.skipped.append((iri, "fetch failed or off-origin"))
            continue

        # Enqueue LDP-contained members (on-origin only).
        for member in graph.objects(URIRef(iri), LDP.contains):
            candidate = str(member)
            if candidate not in visited and _under(candidate, source):
                queue.append(candidate)

        dest = rebased(iri, source, target) or iri
        remapped = rewrite_graph(graph, source, target)
        record_alternative_identifier(remapped, URIRef(dest), iri)
        created, modified = _provenance(graph, URIRef(iri))

        if not dry_run:
            await repository.write_imported(
                dest,
                remapped,
                subject=None,
                created=created,
                modified=modified,
                state=MetadataState.PUBLISHED,
            )
            if validate is not None and (URIRef(dest), DCTERMS.conformsTo, None) in remapped:
                for issue in await validate(dest, remapped):
                    report.validation_issues.append((dest, issue))
        report.imported.append((iri, dest))

    log.info(
        "reference_import_complete",
        source=source,
        target=target,
        imported=report.count,
        skipped=len(report.skipped),
        issues=len(report.validation_issues),
        dry_run=dry_run,
    )
    return report


def _under(iri: str, base: str) -> bool:
    """Whether ``iri`` is ``base`` itself or sits beneath it (egress + walk bound)."""
    return iri == base or any(iri.startswith(base + sep) for sep in ("/", "#", "?"))


async def _fetch(http_client: httpx.AsyncClient, iri: str, source: str) -> Graph | None:
    """GET ``iri`` (on-origin only) and parse it, or ``None`` on any failure."""
    if not _under(iri, source):
        return None
    try:
        response = await http_client.get(
            iri, headers={"accept": _RDF_ACCEPT}, follow_redirects=True
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    graph = Graph()
    try:
        graph.parse(data=response.text, format=_rdflib_format(response.headers.get("content-type")))
    except Exception:
        return None
    return graph


def _rdflib_format(content_type: str | None) -> str:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return {
        "text/turtle": "turtle",
        "application/ld+json": "json-ld",
        "application/rdf+xml": "xml",
        "application/n-triples": "nt",
    }.get(media, "turtle")


def _provenance(graph: Graph, subject: URIRef) -> tuple[datetime, datetime]:
    """The source's (created, modified) from dct:issued/created + dct:modified.

    Falls back to ``now`` for either when the source is silent, so an imported
    record always has coherent meta timestamps.
    """
    now = datetime.now(UTC)
    created = _first_datetime(graph, subject, _ISSUED) or now
    modified = _first_datetime(graph, subject, _MODIFIED) or created
    return created, modified


def _first_datetime(
    graph: Graph, subject: URIRef, predicates: tuple[URIRef, ...]
) -> datetime | None:
    for predicate in predicates:
        for obj in graph.objects(subject, predicate):
            try:
                parsed = datetime.fromisoformat(str(obj))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


__all__ = ["ImportReport", "import_reference_fdp"]
