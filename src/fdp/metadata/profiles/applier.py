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

Apply order (architecture §12.2): schemas → offers → root Repository
seed → seed records. Resource-definition processing happens between
offers and the Repository seed because the seed needs to know the
root RD's schema IRI and which offer is the system default.

**IRI conventions**

* Schema IRI: CURIE expansion via :class:`IRIExpander`.
* Offer IRI: intrinsic — the subject of the ``odrl:Offer`` triple in
  the bundled TTL. Stable across deployments.
* Root Repository IRI: the deployment's ``base_url`` itself (no
  trailing slash). The Repository LDP container lives at the API root.
* Seed record IRI: ``{base_url}/{seed_id}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.metadata.profiles.iri import IRIExpander
from fdp.metadata.profiles.rd_records import (
    RD_SHAPE_IRI,
    predefined_shape_graph,
    rd_record_slug,
    record_to_graph,
)
from fdp.metadata.profiles.registry import (
    ResourceDefinitionCache,
    build_cache_from_manifest,
    records_from_manifest,
)
from fdp.metadata.profiles.validator import (
    ValidationReport,
    validate_profile,
)
from fdp.metadata.states import SEED_STATE
from fdp.shared.errors import BadRequest, Conflict, FDPError
from fdp.shared.graphs import resource_definition_graph_uri
from fdp.shared.namespaces import DCT, LDP, ODRL

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from fdp.config import Settings
    from fdp.metadata.profiles.manifest import DeploymentProfile, LoadedOffer
    from fdp.metadata.profiles.state import ProfileStateRepository
    from fdp.metadata.repository import MetadataRepository

log = structlog.get_logger(__name__)


# --- exceptions ------------------------------------------------------------


class ApplyError(FDPError):
    """Raised when an apply attempt cannot complete."""

    code = "fdp.profile_apply_failed"
    http_status = 500
    docs_url = "https://specs.fairdatapoint.org/errors#fdp.profile_apply_failed"


# --- report --------------------------------------------------------------


@dataclass
class ApplyReport:
    """Outcome of a successful or attempted apply.

    ``resource_definitions`` is set on success when the manifest declared
    any. Callers (CLI / lifespan auto-bootstrap) hand it to
    ``app.state.resource_definitions`` so the LDP router's container
    registry and the OpenAPI generator can read it.

    ``system_default_offer_iri`` is set when the manifest declared an
    offer flagged ``isSystemDefault: true``. Auto-bootstrap installs
    it on the resolver so records that don't carry an explicit
    ``dct:rights`` (and don't link back to the Repository via
    ``dct:isPartOf``) still resolve to the deployment default.
    """

    schemas_written: list[str] = field(default_factory=list)
    offers_written: list[str] = field(default_factory=list)
    rd_shape_iri: str | None = None
    resource_definition_records: list[str] = field(default_factory=list)
    repository_iri: str | None = None
    seed_records_written: list[str] = field(default_factory=list)
    resource_definitions: ResourceDefinitionCache | None = None
    system_default_offer_iri: str | None = None
    rolled_back: bool = False

    @property
    def total_written(self) -> int:
        return (
            len(self.schemas_written)
            + len(self.offers_written)
            + (1 if self.rd_shape_iri else 0)
            + len(self.resource_definition_records)
            + (1 if self.repository_iri else 0)
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
    for calling :meth:`ProfileStateRepository.clear` plus triple-store
    cleanup before invoking this. ``force=True`` here only suppresses
    the already-initialized refusal so a clean deployment can re-apply.
    """
    if not force and await state.is_applied():
        raise Conflict(
            "profile already applied; use force-apply to re-bootstrap",
            details={"profile": profile.name},
        )

    pre = validate_profile(profile)
    if not pre.ok:
        raise _bad_validation(pre)

    report = ApplyReport()
    written: list[str] = []
    expander = IRIExpander(settings=settings)

    try:
        # 1. SHACL schemas — stored at their CURIE-expanded IRI.
        for loaded in profile.schemas:
            iri = expander.schema_iri(loaded.entry.id)
            await repository.put_graph(iri, loaded.graph, subject=None, initial_state=SEED_STATE)
            report.schemas_written.append(iri)
            written.append(iri)

        # 2. ODRL Offers — stored at their intrinsic file-declared IRI.
        offer_iris: dict[str, str] = {}  # offer-entry id → IRI
        for offer in profile.offers:
            iri = _offer_iri_from_graph(offer.graph)
            await repository.put_graph(iri, offer.graph, subject=None, initial_state=SEED_STATE)
            offer_iris[offer.entry.id] = iri
            report.offers_written.append(iri)
            written.append(iri)

        # 2a. Resource-definition records (ADR-0009). The predefined RD SHACL
        #     shape (server-owned, fixed IRI) is stored so the validator can
        #     resolve it, then each manifest resource definition is written as
        #     an RDF record under the reserved resource-definitions namespace.
        #     These are the runtime source of truth; the in-memory cache below
        #     is a projection (task #3 rebuilds it from these records).
        if profile.manifest.resource_definitions:
            await repository.put_graph(RD_SHAPE_IRI, predefined_shape_graph(), subject=None, initial_state=SEED_STATE)
            report.rd_shape_iri = RD_SHAPE_IRI
            written.append(RD_SHAPE_IRI)
            records = records_from_manifest(
                profile.manifest.resource_definitions, expander=expander
            )
            for record in records:
                iri = str(
                    resource_definition_graph_uri(
                        expander.base_url, rd_record_slug(record.url_prefix, record.name)
                    )
                )
                await repository.put_graph(iri, record_to_graph(record, iri), subject=None, initial_state=SEED_STATE)
                report.resource_definition_records.append(iri)
                written.append(iri)

        # 3. Resource-definition cache — derived from the manifest, then
        #    handed to callers via ApplyReport for installation on app.state.
        rd_cache: ResourceDefinitionCache | None = None
        if profile.manifest.resource_definitions:
            rd_cache = build_cache_from_manifest(
                profile.manifest.resource_definitions,
                expander=expander,
            )

        # 4. Root Repository seed. Only emitted when the manifest declares
        #    a root resource definition. Carries dct:title (from the
        #    profile metadata) so the Repository SHACL shape's title
        #    requirement is satisfied; dct:rights points at the
        #    system-default Offer when one is declared.
        system_default_iri = _find_system_default_offer(profile.offers, offer_iris)
        report.system_default_offer_iri = system_default_iri
        if rd_cache is not None:
            root_rd = rd_cache.root()
            if root_rd is not None:
                repo_iri = expander.base_url
                graph = _repository_graph(
                    iri=repo_iri,
                    schema_iri=root_rd.schema_iri,
                    title=profile.name,
                    rights_iri=system_default_iri,
                )
                await repository.put_graph(repo_iri, graph, subject=None, initial_state=SEED_STATE)
                report.repository_iri = repo_iri
                written.append(repo_iri)

        # 5. Explicit seed records.
        for seed in profile.seed_records:
            iri = expander.seed_record_iri(seed.entry.id)
            await repository.put_graph(iri, seed.graph, subject=None, initial_state=SEED_STATE)
            report.seed_records_written.append(iri)
            written.append(iri)

        # 6. Persist the applied-profile marker last so a mid-flight
        #    failure leaves the system uninitialized.
        await state.record(
            name=profile.name,
            version=profile.version,
            manifest_checksum=profile.manifest_checksum,
        )
        await session.commit()
    except Exception as err:  # any failure → compensation rollback
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

    report.resource_definitions = rd_cache
    log.info(
        "profile_apply_succeeded",
        profile=profile.name,
        version=profile.version,
        written=report.total_written,
    )
    return report


def resolve_runtime_state(
    profile: DeploymentProfile,
    *,
    settings: Settings,
) -> tuple[str | None, ResourceDefinitionCache | None]:
    """Derive the runtime state an apply would publish, without touching storage.

    Returns ``(system_default_offer_iri, resource_definitions)``. Used on
    startup when the profile is *already applied* (so :func:`apply_profile` is
    skipped) to repopulate the offer resolver's fallback, the LDP container
    registry, and the OpenAPI generator — these live in ``app.state`` and would
    otherwise be ``None`` after a restart. Pure: derives everything from the
    loaded profile + IRI expansion, the same way ``apply_profile`` does.
    """
    expander = IRIExpander(settings=settings)
    offer_iris = {offer.entry.id: _offer_iri_from_graph(offer.graph) for offer in profile.offers}
    system_default_iri = _find_system_default_offer(profile.offers, offer_iris)
    rd_cache: ResourceDefinitionCache | None = None
    if profile.manifest.resource_definitions:
        rd_cache = build_cache_from_manifest(
            profile.manifest.resource_definitions,
            expander=expander,
        )
    return system_default_iri, rd_cache


# --- helpers --------------------------------------------------------------


def _bad_validation(report: ValidationReport) -> BadRequest:
    return BadRequest(
        "profile failed structural validation",
        details={
            "issues": [
                {"where": i.where, "code": i.code, "message": i.message} for i in report.issues
            ]
        },
    )


def _offer_iri_from_graph(graph: Graph) -> str:
    """Return the (single) ``odrl:Offer`` subject URI from ``graph``.

    The validator already guarantees one Offer subject per file.
    """
    for subject in graph.subjects(RDF.type, ODRL.Offer):
        if isinstance(subject, URIRef):
            return str(subject)
    raise ApplyError(
        "offer graph has no odrl:Offer subject (validator should have caught this)",
        details={},
    )


def _find_system_default_offer(
    offers: tuple[LoadedOffer, ...] | list[LoadedOffer],
    offer_iris: dict[str, str],
) -> str | None:
    """Return the IRI of the offer flagged ``isSystemDefault: true``."""
    for offer in offers:
        if offer.entry.is_system_default:
            return offer_iris.get(offer.entry.id)
    return None


def _repository_graph(
    *,
    iri: str,
    schema_iri: str,
    title: str,
    rights_iri: str | None,
) -> Graph:
    """Build the seed graph for the root Repository record.

    The Repository is the single mandatory FDP resource (architecture
    §10). Sat at the API root, typed as both the root RD's schema
    class and ``ldp:BasicContainer``, with a human-readable title and
    an optional link to the system-default Offer for inheritance.
    """
    subject = URIRef(iri)
    graph = Graph()
    graph.add((subject, RDF.type, URIRef(schema_iri)))
    graph.add((subject, RDF.type, LDP.BasicContainer))
    graph.add((subject, DCT.title, Literal(title)))
    if rights_iri is not None:
        graph.add((subject, DCT.rights, URIRef(rights_iri)))
    return graph


__all__ = ["ApplyError", "ApplyReport", "apply_profile"]
