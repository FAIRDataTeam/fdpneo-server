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

Apply order (architecture §12.2): schemas → offers → default licenses →
root Repository seed → seed records. Resource-definition processing happens
between offers and the Repository seed because the seed needs to know the
root RD's schema IRI and which offer is the system default.

**IRI conventions**

* Schema IRI: CURIE expansion via :class:`IRIExpander`.
* Offer IRI: a managed-policy IRI ``{base_url}/policies/{offer_id}`` (ADR-0012).
  The bundled TTL declares the Offer at an intrinsic IRI; the applier rewrites
  the Offer subject to the deployment-local managed IRI and stores it there, so
  the Offer is dereferenceable (``GET /policies/{id}``) and ``dct:rights``
  references resolve locally.
* License IRI: ``{base_url}/licenses/{license_id}`` — the built-in default set
  (ADR-0012), referenced descriptively via ``dct:license``.
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

from fdp.metadata.licenses import LICENSE_SHAPE_IRI, predefined_license_shape_graph
from fdp.metadata.meta import META_SHAPE_IRI
from fdp.metadata.profiles.iri import IRIExpander, expand_schema_refs
from fdp.metadata.profiles.licenses import default_license_graphs
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
from fdp.shared.graphs import policy_graph_uri, resource_definition_graph_uri
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
    licenses_written: list[str] = field(default_factory=list)
    license_shape_iri: str | None = None
    meta_shape_iri: str | None = None
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
            + len(self.licenses_written)
            + (1 if self.license_shape_iri else 0)
            + (1 if self.meta_shape_iri else 0)
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
        # 0. Meta-metadata shape — stored at its fixed IRI (META_SHAPE_IRI) so
        #    runtime meta validation (MetaWriter) can fetch it. Written first so
        #    the very next meta refresh has a shape to validate against. Without
        #    this the shape exists only for bootstrap-time profile validation.
        if profile.meta_metadata_schema is not None:
            await repository.put_graph(
                META_SHAPE_IRI,
                profile.meta_metadata_schema,
                subject=None,
                initial_state=SEED_STATE,
            )
            report.meta_shape_iri = META_SHAPE_IRI
            written.append(META_SHAPE_IRI)

        # 1. SHACL schemas — stored under the reserved schemas namespace
        #    ({base}/fdp-api/schemas/{slug}), the same place a runtime
        #    PUT /schemas/{id} writes, so a profile schema is thereafter an
        #    ordinary editable/listable user schema (task 10.5). The shape's
        #    node IRI + sh:targetClass inside the graph stay the class IRI, so
        #    SHACL matching is unaffected; only the storage/fetch key moves.
        for loaded in profile.schemas:
            iri = expander.schema_storage_iri(loaded.entry.id)
            # Expand urn:fdp-schema:<slug> placeholders to storage IRIs so a
            # modular shape's own subject + its sh:node references resolve to
            # where the referenced base shapes are actually stored (task 15.2).
            graph = expand_schema_refs(loaded.graph, expander.base_url)
            await repository.put_graph(iri, graph, subject=None, initial_state=SEED_STATE)
            report.schemas_written.append(iri)
            written.append(iri)

        # 2. ODRL Offers — seeded as managed policy documents (ADR-0012). The
        #    bundled TTL declares the Offer at an intrinsic IRI; we rewrite the
        #    subject to the deployment-local managed IRI {base}/policies/{id} and
        #    store the graph there, so it is dereferenceable and dct:rights
        #    references resolve locally.
        offer_iris: dict[str, str] = {}  # offer-entry id → managed policy IRI
        for offer in profile.offers:
            intrinsic = URIRef(_offer_iri_from_graph(offer.graph))
            managed = _managed_policy_iri(expander, offer.entry.id)
            graph = _rewrite_subject(offer.graph, intrinsic, URIRef(managed))
            await repository.put_graph(managed, graph, subject=None, initial_state=SEED_STATE)
            offer_iris[offer.entry.id] = managed
            report.offers_written.append(managed)
            written.append(managed)

        # 2b. License SHACL shape (server-owned, fixed IRI) so PUT /licenses can
        #     validate against it, then the built-in default license documents
        #     (ADR-0012) at {base}/licenses/{id}.
        await repository.put_graph(
            LICENSE_SHAPE_IRI,
            predefined_license_shape_graph(),
            subject=None,
            initial_state=SEED_STATE,
        )
        report.license_shape_iri = LICENSE_SHAPE_IRI
        written.append(LICENSE_SHAPE_IRI)
        for lic_iri, lic_graph in default_license_graphs(expander.base_url):
            await repository.put_graph(lic_iri, lic_graph, subject=None, initial_state=SEED_STATE)
            report.licenses_written.append(lic_iri)
            written.append(lic_iri)

        # 2a. Resource-definition records (ADR-0009). The predefined RD SHACL
        #     shape (server-owned, fixed IRI) is stored so the validator can
        #     resolve it, then each manifest resource definition is written as
        #     an RDF record under the reserved resource-definitions namespace.
        #     These are the runtime source of truth; the in-memory cache below
        #     is a projection (task #3 rebuilds it from these records).
        if profile.manifest.resource_definitions:
            await repository.put_graph(
                RD_SHAPE_IRI, predefined_shape_graph(), subject=None, initial_state=SEED_STATE
            )
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
                await repository.put_graph(
                    iri, record_to_graph(record, iri), subject=None, initial_state=SEED_STATE
                )
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
                # The seed's rdf:type must be the *class* IRI (so the shape's
                # sh:targetClass matches), not the schema's storage IRI that the
                # RD's ldp:constrainedBy now uses (10.5). Derive it from the root
                # manifest entry's schema CURIE.
                root_entry = next(
                    (rd for rd in profile.manifest.resource_definitions if rd.is_root),
                    None,
                )
                root_class_iri = (
                    expander.schema_iri(root_entry.schema_id)
                    if root_entry is not None
                    else root_rd.schema_iri
                )
                graph = _repository_graph(
                    iri=repo_iri,
                    type_iri=root_class_iri,
                    member_relations=[c.relation_uri for c in root_rd.children],
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
    offer_iris = {
        offer.entry.id: _managed_policy_iri(expander, offer.entry.id) for offer in profile.offers
    }
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


def _managed_policy_iri(expander: IRIExpander, offer_id: str) -> str:
    """The deployment-local managed-policy IRI for a profile offer (ADR-0012)."""
    return str(policy_graph_uri(expander.base_url, offer_id))


def _rewrite_subject(graph: Graph, old: URIRef, new: URIRef) -> Graph:
    """Return a copy of ``graph`` with node ``old`` renamed to ``new``.

    Used to move a bundled Offer from its intrinsic IRI to the deployment-local
    managed-policy IRI so the stored graph is self-consistent (``<new> a
    odrl:Offer``) and dereferenceable. Rewrites both subject and object
    positions; the Offer's rules are blank nodes and are left untouched.
    """
    rewritten = Graph()
    for s, p, o in graph:
        rewritten.add(
            (
                new if s == old else s,
                p,
                new if o == old else o,
            )
        )
    return rewritten


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
    type_iri: str,
    member_relations: list[str],
    title: str,
    rights_iri: str | None,
) -> Graph:
    """Build the seed graph for the root record (the FAIR Data Point).

    The root is the single mandatory FDP resource (architecture §10). Sat at the
    API root, typed as the root RD's schema *class* (``type_iri`` — so the
    shape's ``sh:targetClass`` matches) and as a genuine LDP **Direct Container**
    (ADR-0008, task 15.1): it carries the membership configuration
    (``ldp:membershipResource`` = itself, one ``ldp:hasMemberRelation`` per RD
    child relation, ``ldp:insertedContentRelation ldp:MemberSubject``) so a
    standards consumer reads the container's membership pattern directly.
    """
    subject = URIRef(iri)
    graph = Graph()
    graph.add((subject, RDF.type, URIRef(type_iri)))
    graph.add((subject, DCT.title, Literal(title)))
    if rights_iri is not None:
        graph.add((subject, DCT.rights, URIRef(rights_iri)))
    for triple in direct_container_config(subject, member_relations):
        graph.add(triple)
    return graph


def direct_container_config(
    subject: URIRef, member_relations: list[str]
) -> list[tuple[URIRef, URIRef, URIRef]]:
    """LDP Direct Container membership triples for a container ``subject``.

    Declares the container an ``ldp:DirectContainer`` whose members are reached
    from the container itself (``ldp:membershipResource``) via each typed
    relation (``ldp:hasMemberRelation`` — one per RD child link, e.g.
    ``fdp-o:servesMetadata`` / ``dcat:dataset``), with the posted resource as the
    member (``ldp:insertedContentRelation ldp:MemberSubject``). Shared by the
    root seed and runtime container records so every FDP container is a genuine
    Direct Container.
    """
    triples: list[tuple[URIRef, URIRef, URIRef]] = [
        (subject, RDF.type, LDP.DirectContainer),
        (subject, LDP.membershipResource, subject),
        (subject, LDP.insertedContentRelation, LDP.MemberSubject),
    ]
    triples += [(subject, LDP.hasMemberRelation, URIRef(rel)) for rel in member_relations]
    return triples


__all__ = ["ApplyError", "ApplyReport", "apply_profile", "direct_container_config"]
