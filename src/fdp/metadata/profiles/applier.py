"""Profile bootstrap pipeline (architecture §12.2).

Applies a validated :class:`DeploymentProfile` to the triple store and
records the ``profile_applied`` marker in Postgres. The pipeline is
not a database-level transaction across both stores — the SPARQL 1.1
Protocol's transaction guarantees vary by backend (ADR-0005) — so we
emulate atomicity with **compensation rollback**:

* Every graph the applier writes is tracked.
* On any failure (validation surprise, triple-store error, integrity
  error on the marker row), every tracked graph is dropped through
  :meth:`MetadataRepository.delete_graph`, which also removes the
  sibling meta + audit graphs.
* The ``profile_applied`` row is the **last** thing written, so a
  rollback always leaves the system "uninitialized" — a subsequent
  apply can re-attempt without ``--force``.

Apply order is the one architecture §12.2 specifies: schemas →
containers → offers → seed records.

**IRI conventions** (kept narrow on purpose; profiles authored against
this scheme remain portable):

* Schema IRI: CURIE expansion through
  :data:`fdp.shared.namespaces.PREFIXES` plus the deployment's
  ``fdp:`` namespace. ``fdp:Repository`` → ``<fdp_namespace>Repository``;
  ``dcat:Catalog`` → ``http://www.w3.org/ns/dcat#Catalog``.
* Offer IRI: ``{base_url}/offers/{id}``.
* Container IRI: ``{base_url}/{id}``.
* Seed record IRI: the seed record's ``id`` is itself a relative IRI
  appended to the base URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF

from fdp.metadata.profiles.manifest import ContainerEntry
from fdp.metadata.profiles.validator import (
    ValidationReport,
    validate_profile,
)
from fdp.shared.errors import BadRequest, Conflict, FDPError
from fdp.shared.namespaces import DCT, LDP, PREFIXES, fdp_namespace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from fdp.config import Settings
    from fdp.metadata.profiles.manifest import DeploymentProfile
    from fdp.metadata.profiles.state import ProfileStateRepository
    from fdp.metadata.repository import MetadataRepository

log = structlog.get_logger(__name__)


# --- exceptions ------------------------------------------------------------


class ApplyError(FDPError):
    """Raised when an apply attempt cannot complete.

    Wraps the underlying cause; ``details`` carries a structured
    description so the CLI can render something better than a stack
    trace.
    """

    code = "fdp.profile_apply_failed"
    http_status = 500
    docs_url = "https://specs.fairdatapoint.org/errors#fdp.profile_apply_failed"


# --- report --------------------------------------------------------------


@dataclass
class ApplyReport:
    """Outcome of a successful or attempted apply."""

    schemas_written: list[str] = field(default_factory=list)
    containers_written: list[str] = field(default_factory=list)
    offers_written: list[str] = field(default_factory=list)
    seed_records_written: list[str] = field(default_factory=list)
    rolled_back: bool = False

    @property
    def total_written(self) -> int:
        return (
            len(self.schemas_written)
            + len(self.containers_written)
            + len(self.offers_written)
            + len(self.seed_records_written)
        )


# --- main entry point ---------------------------------------------------


async def apply_profile(
    profile: DeploymentProfile,
    *,
    repository: MetadataRepository,
    state: ProfileStateRepository,
    session: AsyncSession,
    settings: Settings,
    force: bool = False,
) -> ApplyReport:
    """Apply ``profile`` to the configured stores.

    ``force`` does **not** wipe state by itself; the CLI is responsible
    for calling :meth:`ProfileStateRepository.clear` plus
    triple-store cleanup before invoking this. Setting ``force=True``
    here only suppresses the already-initialized refusal so a clean
    deployment can re-apply.
    """
    if not force:
        if await state.is_applied():
            raise Conflict(
                "profile already applied; use force-apply to re-bootstrap",
                details={"profile": profile.name},
            )

    pre = validate_profile(profile)
    if not pre.ok:
        raise _bad_validation(pre)

    report = ApplyReport()
    written: list[str] = []
    expander = _IRIExpander(settings=settings)

    try:
        for loaded in profile.schemas:
            iri = expander.schema_iri(loaded.entry.id)
            await repository.put_graph(iri, loaded.graph, subject=None)
            report.schemas_written.append(iri)
            written.append(iri)

        for container in profile.manifest.containers:
            iri = expander.container_iri(container.id)
            await repository.put_graph(
                iri,
                _container_graph(container, expander),
                subject=None,
            )
            report.containers_written.append(iri)
            written.append(iri)

        for offer in profile.offers:
            iri = expander.offer_iri(offer.entry.id)
            await repository.put_graph(iri, offer.graph, subject=None)
            report.offers_written.append(iri)
            written.append(iri)

        for seed in profile.seed_records:
            iri = expander.seed_record_iri(seed.entry.id)
            await repository.put_graph(iri, seed.graph, subject=None)
            report.seed_records_written.append(iri)
            written.append(iri)

        await state.record(
            name=profile.name,
            version=profile.version,
            manifest_checksum=profile.manifest_checksum,
        )
        await session.commit()
    except Exception as err:  # any failure triggers compensation rollback
        log.error(
            "profile_apply_failed",
            profile=profile.name,
            written=len(written),
            error=repr(err),
        )
        await session.rollback()
        for iri in reversed(written):
            try:
                await repository.delete_graph(iri)
            except Exception as drop_err:
                log.warning(
                    "profile_rollback_drop_failed",
                    iri=iri,
                    error=repr(drop_err),
                )
        report.rolled_back = True
        if isinstance(err, FDPError):
            raise
        raise ApplyError(
            f"profile apply failed: {err}",
            details={"profile": profile.name, "written_graphs": len(written)},
        ) from err

    log.info(
        "profile_apply_succeeded",
        profile=profile.name,
        version=profile.version,
        written=report.total_written,
    )
    return report


# --- helpers --------------------------------------------------------------


def _bad_validation(report: ValidationReport) -> BadRequest:
    return BadRequest(
        "profile failed structural validation",
        details={
            "issues": [
                {"where": i.where, "code": i.code, "message": i.message}
                for i in report.issues
            ]
        },
    )


class _IRIExpander:
    """Translates manifest-local identifiers (CURIEs, slugs) into full IRIs.

    Centralized so the URI scheme is in one place — the convention is
    documented in this module's docstring and on each method.
    """

    def __init__(self, *, settings: Settings) -> None:
        self._base = str(settings.base_url).rstrip("/")
        self._prefixes = dict(PREFIXES)
        self._fdp = fdp_namespace(settings)

    def schema_iri(self, schema_id: str) -> str:
        """Expand a CURIE like ``fdp:Repository``. Unknown prefixes raise."""
        if ":" not in schema_id or schema_id.startswith(("http://", "https://")):
            return schema_id
        prefix, local = schema_id.split(":", 1)
        if prefix == "fdp":
            return str(self._fdp[local])
        ns: Namespace | None = self._prefixes.get(prefix)
        if ns is None:
            raise BadRequest(
                f"unknown prefix in schema id: {prefix}",
                details={"schema_id": schema_id},
            )
        return str(ns[local])

    def offer_iri(self, offer_id: str) -> str:
        return f"{self._base}/offers/{offer_id}"

    def container_iri(self, container_id: str) -> str:
        return f"{self._base}/{container_id}"

    def seed_record_iri(self, seed_id: str) -> str:
        seed_id = seed_id.lstrip("/")
        return f"{self._base}/{seed_id}"


def _container_graph(container: ContainerEntry, expander: _IRIExpander) -> Graph:
    """Build a tiny LDP container graph (architecture §10 / §12.1).

    A profile-declared container is materialized as an LDP
    ``BasicContainer`` whose ``ldp:constrainedBy`` links to the schema
    its members must satisfy. The parent link is recorded as
    ``dct:isPartOf`` so the container hierarchy is queryable; LDP's
    containment triples accumulate as members are added at runtime.
    """
    subject = URIRef(expander.container_iri(container.id))
    graph = Graph()
    graph.add((subject, RDF.type, LDP.BasicContainer))
    graph.add((subject, RDF.type, URIRef(expander.schema_iri(container.type))))
    if container.constrained_by is not None:
        graph.add(
            (
                subject,
                LDP.constrainedBy,
                URIRef(expander.schema_iri(container.constrained_by)),
            )
        )
    if container.parent is not None:
        graph.add(
            (subject, DCT.isPartOf, URIRef(expander.container_iri(container.parent)))
        )
    return graph


__all__ = ["ApplyError", "ApplyReport", "apply_profile"]
