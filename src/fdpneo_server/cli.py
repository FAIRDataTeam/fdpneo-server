"""FDP command-line interface.

Exposes administrative commands for managing a deployment: bootstrap with
a deployment profile, inspect applied state, and export current state as a
distributable profile.

Subcommands:

* ``fdp profile validate <path>`` — dry-run validation of a profile bundle.
* ``fdp profile apply <path>``    — bootstrap (refuses if already initialized).
* ``fdp profile info``            — show the applied profile name and version.
* ``fdp profile export <path>``   — deferred to v1.x.
* ``fdp db migrate``              — run Alembic migrations.
* ``fdp metrics rollup``          — run metrics rollups (cron-driven).
* ``fdp schema sync``             — refetch remote-sourced schemas (cron-driven).
* ``fdp schema migrate-namespace`` — relocate profile schemas into the
  schemas namespace (one-shot, for pre-10.5 deployments).
* ``fdp search reindex``          — rebuild the metadata search index.

The CLI is built with Typer for argument parsing and Rich for output.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from fdpneo_server.config import Settings
    from fdpneo_server.metadata.backup import DumpResult, ImportReport, RestoreResult
    from fdpneo_server.metadata.index_ping import PingResult
    from fdpneo_server.metadata.pid.github import PublishResult
    from fdpneo_server.metadata.pid.rebase import RebaseReport
    from fdpneo_server.metadata.pid.verify import ResolutionReport
    from fdpneo_server.metadata.prof_backfill import ConformanceBackfillReport
    from fdpneo_server.metadata.profiles.applier import ApplyReport
    from fdpneo_server.metadata.profiles.backfill import MembershipBackfillReport
    from fdpneo_server.metadata.profiles.manifest import DeploymentProfile
    from fdpneo_server.metadata.profiles.migrate import MigrationReport
    from fdpneo_server.metadata.profiles.migrate_modular import ModularMigrationReport
    from fdpneo_server.metadata.schema_sync import SyncReport
    from fdpneo_server.metrics.aggregation import RollupResult

app = typer.Typer(
    name="fdp",
    help="FAIR Data Point administrative CLI",
    no_args_is_help=True,
    add_completion=False,
)

profile_app = typer.Typer(help="Deployment-profile commands.", no_args_is_help=True)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
metrics_app = typer.Typer(help="Metrics-pipeline commands.", no_args_is_help=True)
schema_app = typer.Typer(help="Schema commands.", no_args_is_help=True)
search_app = typer.Typer(help="Search commands.", no_args_is_help=True)
ldp_app = typer.Typer(help="LDP-conformance commands.", no_args_is_help=True)
pid_app = typer.Typer(help="Persistent-identifier commands.", no_args_is_help=True)
backup_app = typer.Typer(help="Backup / restore / migration commands.", no_args_is_help=True)
index_app = typer.Typer(help="Index / discovery commands.", no_args_is_help=True)

app.add_typer(profile_app, name="profile")
app.add_typer(db_app, name="db")
app.add_typer(metrics_app, name="metrics")
app.add_typer(schema_app, name="schema")
app.add_typer(search_app, name="search")
app.add_typer(ldp_app, name="ldp")
app.add_typer(pid_app, name="pid")
app.add_typer(backup_app, name="backup")
app.add_typer(index_app, name="index")

console = Console()


# --- profile commands ----------------------------------------------------


@profile_app.command("validate")
def profile_validate(
    path: Path | None = typer.Argument(None, exists=True, file_okay=False),
) -> None:
    """Validate a profile bundle (default: the bundled DCAT profile)."""
    from fdpneo_server.metadata.profiles import (
        bundled_default_profile,
        load_profile,
        validate_profile,
    )
    from fdpneo_server.shared.errors import FDPError

    try:
        profile = load_profile(path if path is not None else bundled_default_profile())
    except FDPError as err:
        console.print(f"[red]profile load failed:[/] {err.message}")
        raise typer.Exit(code=1) from err

    report = validate_profile(profile)
    if report.ok:
        console.print(
            f"[green]ok[/] {profile.name} {profile.version} — "
            f"{len(profile.schemas)} schemas, "
            f"{len(profile.offers)} offers, "
            f"{len(profile.manifest.resource_definitions)} resource definitions, "
            f"{len(profile.seed_records)} seed records"
        )
        return

    console.print(f"[red]{len(report.issues)} validation issue(s):[/]")
    for issue in report.issues:
        console.print(f"  [yellow]{issue.where}[/] ({issue.code}): {issue.message}")
    raise typer.Exit(code=1)


@profile_app.command("apply")
def profile_apply(
    path: Path | None = typer.Argument(None, exists=True, file_okay=False),
    force: bool = typer.Option(
        False,
        "--force",
        help="Wipe the existing applied profile (including ALL named graphs) before applying.",
    ),
) -> None:
    """Apply a profile to bootstrap an uninitialized FDP (default: the bundled DCAT profile)."""
    from fdpneo_server.metadata.profiles import bundled_default_profile, load_profile
    from fdpneo_server.shared.errors import FDPError

    try:
        profile = load_profile(path if path is not None else bundled_default_profile())
    except FDPError as err:
        console.print(f"[red]profile load failed:[/] {err.message}")
        raise typer.Exit(code=1) from err

    if force:
        confirmed = typer.confirm(
            "Force-apply wipes the entire metadata triple store and the "
            "profile_applied marker. This is destructive. Proceed?",
            default=False,
        )
        if not confirmed:
            console.print("aborted")
            raise typer.Exit(code=1)

    try:
        report = asyncio.run(_run_apply(profile, force=force))
    except FDPError as err:
        console.print(f"[red]apply failed:[/] {err.message}")
        raise typer.Exit(code=1) from err

    rd_count = (
        len(report.resource_definitions.all()) if report.resource_definitions is not None else 0
    )
    console.print(
        f"[green]applied[/] {profile.name} {profile.version} — "
        f"{len(report.schemas_written)} schemas, "
        f"{len(report.offers_written)} offers, "
        f"{rd_count} resource definitions, "
        f"{len(report.seed_records_written)} seed records"
    )


@profile_app.command("info")
def profile_info() -> None:
    """Show the applied profile name and version."""
    from fdpneo_server.shared.errors import FDPError

    try:
        info = asyncio.run(_load_applied())
    except FDPError as err:
        console.print(f"[red]info failed:[/] {err.message}")
        raise typer.Exit(code=1) from err
    if info is None:
        console.print("[yellow]no profile applied[/]")
        raise typer.Exit(code=0)
    console.print(
        f"[green]{info['name']} {info['version']}[/] "
        f"applied at {info['applied_at']} "
        f"(checksum {info['manifest_checksum'][:12]}…)"
    )


@profile_app.command("export")
def profile_export(path: Path = typer.Argument(..., file_okay=False)) -> None:
    """Export the current FDP state as a distributable profile.

    Deferred to v1.x — exporting a round-trippable profile bundle
    requires solving cross-bundle inheritance and the "what counts as
    bundle-local state" question (architecture §12.4). The CLI keeps
    the command surface so tooling can detect availability.
    """
    console.print(f"[yellow]profile export is deferred to v1.x[/] (would have written to {path})")
    raise typer.Exit(code=1)


@profile_app.command("migrate-modular")
def profile_migrate_modular(
    path: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    """Reconcile a deployment to the modular DCAT-3/FDP-O profile (task 15.2).

    Non-destructive, one-shot, idempotent. For deployments bootstrapped before the
    modular schema set landed: rewrites the schemas + resource definitions to the
    new bundle and re-types the root record (``fdp:Repository`` →
    ``fdp-o:FAIRDataPoint``) **in place**, preserving authored root metadata and
    member records. Pass the modular profile bundle. Prefer this over a
    ``force-apply`` re-bootstrap in production (which re-seeds the root). A no-op on
    an already-migrated deployment.
    """
    from fdpneo_server.metadata.profiles import load_profile
    from fdpneo_server.shared.errors import FDPError

    try:
        profile = load_profile(path)
    except FDPError as err:
        console.print(f"[red]profile load failed:[/] {err.message}")
        raise typer.Exit(code=1) from err

    try:
        report = asyncio.run(_run_modular_migration(profile))
    except Exception as err:
        console.print(f"[red]modular migration failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if not report.changed:
        console.print("[green]nothing to migrate[/] — deployment already on the modular profile")
        return
    parts = [
        f"{len(report.schemas_written)} schema(s)",
        f"{len(report.resource_definitions_written)} resource definition(s) written",
    ]
    if report.resource_definitions_removed:
        parts.append(f"{len(report.resource_definitions_removed)} orphan RD(s) removed")
    if report.root_retyped:
        parts.append("root re-typed")
    console.print(f"[green]migrated[/] {', '.join(parts)}")


async def _run_modular_migration(profile: DeploymentProfile) -> ModularMigrationReport:
    """Build the runtime collaborators and run one modular-migration pass."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.profiles.migrate_modular import migrate_to_modular_profile
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.metadata.shape_provider import MetadataShapeProvider
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
        repository = MetadataRepository(adapter)
        validator = ShaclValidator(MetadataShapeProvider(repository))
        return await migrate_to_modular_profile(
            profile,
            repository=repository,
            adapter=adapter,
            settings=settings,
            validator=validator,
        )


@profile_app.command("backfill-conformance")
def profile_backfill_conformance() -> None:
    """Backfill the ADR-0019 self-describing binding on existing records + schemas.

    Non-destructive, one-shot, idempotent. Provisions the 1:1 profile (+ immutable
    schema version snapshot) for every managed schema, and stamps
    ``dct:conformsTo`` + ``fdp-o:validatedAgainst`` on every existing record of a
    known type that lacks it — **without** bumping the record's version. Derives
    everything from the store; no profile bundle needed. A fresh bootstrap does
    this automatically; run this for deployments created before the binding
    shipped. A no-op on an already-bound deployment.
    """
    try:
        report = asyncio.run(_run_conformance_backfill())
    except Exception as err:
        console.print(f"[red]conformance backfill failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if not report.changed:
        console.print(f"[green]nothing to backfill[/] — {report.already} record(s) already bound")
        return
    console.print(
        f"[green]backfilled[/] {len(report.profiles_provisioned)} profile(s), "
        f"{len(report.records_stamped)} record(s) stamped "
        f"({report.already} already bound)"
    )


async def _run_conformance_backfill() -> ConformanceBackfillReport:
    """Build the runtime collaborators and run one conformance-backfill pass."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.prof_backfill import backfill_conformance
    from fdpneo_server.metadata.profiles import build_cache_from_repository
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
        repository = MetadataRepository(adapter)
        cache = await build_cache_from_repository(
            adapter, base_url=settings.resolved_identifier_base
        )
        return await backfill_conformance(adapter=adapter, repository=repository, cache=cache)


@db_app.command("migrate")
def db_migrate() -> None:
    """Run database migrations to the latest revision."""
    from importlib.resources import files

    from alembic import command
    from alembic.config import Config

    config = Config(str(files("fdpneo_server") / "alembic.ini"))
    command.upgrade(config, "head")
    console.print("[green]migrations applied to head[/]")


# --- metrics commands ----------------------------------------------------


@metrics_app.command("rollup")
def metrics_rollup(
    hourly_only: bool = typer.Option(False, "--hourly-only", help="Run raw → hourly only."),
    daily_only: bool = typer.Option(False, "--daily-only", help="Run hourly → daily only."),
) -> None:
    """Run the metrics rollups once.

    The aggregation logic is the same as ``fdpneo_server.metrics.aggregation`` — we
    just drive it from a CLI invocation instead of an arq worker so
    operators can schedule it externally (cron, k8s ``CronJob``,
    systemd timer). Run the command on the schedule recommended in
    ``MetricsSettings``: roughly every 5 minutes for the raw → hourly
    step, once an hour for hourly → daily.
    """
    if hourly_only and daily_only:
        console.print("[red]--hourly-only and --daily-only are mutually exclusive[/]")
        raise typer.Exit(code=1)

    try:
        raw_result, daily_result = asyncio.run(
            _run_rollup(
                hourly_only=hourly_only,
                daily_only=daily_only,
            )
        )
    except Exception as err:
        console.print(f"[red]rollup failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if raw_result is not None:
        console.print(
            f"[green]raw → hourly[/] "
            f"buckets={raw_result.buckets_processed} "
            f"aggregates={raw_result.aggregates_written} "
            f"raw_deleted={raw_result.source_rows_deleted}"
        )
    if daily_result is not None:
        console.print(
            f"[green]hourly → daily[/] "
            f"days={daily_result.buckets_processed} "
            f"aggregates={daily_result.aggregates_written} "
            f"hourly_deleted={daily_result.source_rows_deleted}"
        )


# --- search commands -----------------------------------------------------


@search_app.command("reindex")
def search_reindex() -> None:
    """Rebuild the metadata search index from the triple store.

    Walks every non-internal record graph and re-derives its search row,
    including the ``anon_read`` visibility flag. Use after a schema/profile
    change, or to repair drift from an inherited-offer change that didn't emit
    a per-record event.
    """
    try:
        count = asyncio.run(_run_search_reindex())
    except Exception as err:
        console.print(f"[red]reindex failed:[/] {err}")
        raise typer.Exit(code=1) from err
    console.print(f"[green]search reindex[/] indexed={count} records")


async def _run_search_reindex() -> int:
    """Rebuild the search index; returns the number of records indexed."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.search.reindex import reindex_all
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
            return await reindex_all(
                adapter,
                session_factory,
                language=settings.search.default_language,
                system_default_offer_iri=_system_default_offer(settings),
            )
    finally:
        await engine.dispose()


def _system_default_offer(settings: Settings) -> str | None:
    """The profile's system-default offer IRI (for anon-read during reindex)."""
    if settings.profile.path is None:
        return None
    from fdpneo_server.metadata.profiles import load_profile, resolve_runtime_state

    system_default, _ = resolve_runtime_state(
        load_profile(settings.profile.path), settings=settings
    )
    return system_default


# --- schema commands -----------------------------------------------------


@schema_app.command("sync")
def schema_sync(
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even when FDP_SCHEMA_SYNC_ENABLED is false (the allow-list still applies).",
    ),
) -> None:
    """Refetch remote-sourced SHACL schemas and republish the changed ones.

    Intended for an external scheduler (cron / k8s ``CronJob``) on the
    ``FDP_SCHEMA_SYNC_INTERVAL_SECONDS`` cadence. The host allow-list
    (``FDP_SCHEMA_SYNC_ALLOWED_HOSTS``) is always enforced; an empty allow-list
    means every fetch is skipped.
    """
    from fdpneo_server.config import get_settings

    settings = get_settings()
    if not settings.schema_sync.enabled and not force:
        console.print(
            "[yellow]schema sync is disabled[/] (set FDP_SCHEMA_SYNC_ENABLED=true or pass --force)"
        )
        raise typer.Exit(code=0)

    try:
        report = asyncio.run(_run_schema_sync())
    except Exception as err:
        console.print(f"[red]schema sync failed:[/] {err}")
        raise typer.Exit(code=1) from err

    console.print(
        f"[green]schema sync[/] "
        f"updated={report.updated} unchanged={report.unchanged} "
        f"skipped={report.skipped} failed={report.failed}"
    )
    if report.failed:
        raise typer.Exit(code=1)


@schema_app.command("migrate-namespace")
def schema_migrate_namespace(
    path: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    """Relocate a profile's schemas into the schemas namespace (task 10.5).

    Non-destructive, one-shot, idempotent. For deployments bootstrapped before
    profile schemas moved to ``{base}/fdp-api/schemas/{slug}``: copies each
    schema graph from its old vocabulary IRI to the new storage IRI, repoints
    resource definitions, and drops the old graph. Pass the same profile bundle
    the deployment was applied with. A no-op on an already-migrated deployment.
    """
    from fdpneo_server.metadata.profiles import load_profile
    from fdpneo_server.shared.errors import FDPError

    try:
        profile = load_profile(path)
    except FDPError as err:
        console.print(f"[red]profile load failed:[/] {err.message}")
        raise typer.Exit(code=1) from err

    try:
        report = asyncio.run(_run_schema_migration(profile))
    except Exception as err:
        console.print(f"[red]schema migration failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if not report.changed:
        console.print("[green]nothing to migrate[/] — schemas already in the schemas namespace")
        return
    console.print(
        f"[green]migrated[/] {len(report.moved)} schema(s), "
        f"repointed {len(report.resource_definitions_repointed)} resource definition(s)"
    )
    for old, new in report.moved:
        console.print(f"  {old} → {new}")


async def _run_schema_migration(profile: DeploymentProfile) -> MigrationReport:
    """Build the runtime collaborators and run one migration pass."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.profiles.migrate import migrate_schema_namespace
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.metadata.shape_provider import MetadataShapeProvider
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
        repository = MetadataRepository(adapter)
        validator = ShaclValidator(MetadataShapeProvider(repository))
        return await migrate_schema_namespace(
            profile,
            repository=repository,
            adapter=adapter,
            settings=settings,
            validator=validator,
        )


@ldp_app.command("backfill-membership")
def ldp_backfill_membership() -> None:
    """Stamp LDP Direct Container membership on pre-15.1 containers.

    Non-destructive, one-shot, idempotent. For deployments bootstrapped before
    containers became genuine ``ldp:DirectContainer``s (task 15.1): walks every
    record, and for each container (a type whose resource definition declares
    child links) adds the membership triad and strips any stale
    ``ldp:BasicContainer`` type. Derives everything from the store; no profile
    bundle needed. A no-op on an already-conformant deployment.
    """
    try:
        report = asyncio.run(_run_membership_backfill())
    except Exception as err:
        console.print(f"[red]membership backfill failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if not report.changed:
        console.print(
            "[green]nothing to backfill[/] — "
            f"{len(report.already_conformant)} container(s) already conformant"
        )
        return
    console.print(
        f"[green]stamped[/] Direct Container membership on {len(report.stamped)} container(s) "
        f"({len(report.already_conformant)} already conformant)"
    )
    for iri in report.stamped:
        console.print(f"  {iri}")


async def _run_membership_backfill() -> MembershipBackfillReport:
    """Build the runtime collaborators and run one backfill pass."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.profiles.backfill import backfill_direct_container_membership
    from fdpneo_server.metadata.profiles.rd_service import build_cache_from_repository
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
        repository = MetadataRepository(adapter)
        cache = await build_cache_from_repository(
            adapter, base_url=settings.resolved_identifier_base
        )
        return await backfill_direct_container_membership(
            repository=repository, adapter=adapter, cache=cache
        )


async def _run_schema_sync() -> SyncReport:
    """Build the runtime collaborators and run one sync pass."""
    import httpx

    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.schema_sync import SchemaSyncService
    from fdpneo_server.metadata.schemas import SchemaService
    from fdpneo_server.metadata.shacl import ShaclValidator
    from fdpneo_server.metadata.shape_provider import MetadataShapeProvider
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with (
        TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter,
        httpx.AsyncClient() as http_client,
    ):
        repository = MetadataRepository(adapter)
        schema_service = SchemaService(
            repository=repository,
            adapter=adapter,
            validator=ShaclValidator(MetadataShapeProvider(repository)),
            base_url=settings.resolved_identifier_base,
        )
        syncer = SchemaSyncService(
            schema_service=schema_service,
            adapter=adapter,
            http_client=http_client,
            settings=settings.schema_sync,
            base_url=settings.resolved_identifier_base,
        )
        return await syncer.sync_all()


async def _run_rollup(
    *,
    hourly_only: bool,
    daily_only: bool,
) -> tuple[RollupResult | None, RollupResult | None]:
    """Drive ``aggregation`` once. Returns (raw_result, daily_result)."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metrics.aggregation import (
        roll_up_hourly_to_daily,
        roll_up_raw_to_hourly,
    )
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        raw_result = None
        daily_result = None
        if not daily_only:
            raw_result = await roll_up_raw_to_hourly(
                session_factory,
                aggregate_after_seconds=settings.metrics.aggregate_to_hourly_after_seconds,
            )
        if not hourly_only:
            daily_result = await roll_up_hourly_to_daily(
                session_factory,
                discard_after_days=settings.metrics.discard_hourly_after_days,
            )
        return raw_result, daily_result
    finally:
        await engine.dispose()


# --- async glue ---------------------------------------------------------


async def _run_apply(profile: DeploymentProfile, *, force: bool) -> ApplyReport:
    """Build the runtime collaborators and call ``apply_profile``."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.profiles.applier import apply_profile
    from fdpneo_server.metadata.profiles.state import ProfileStateRepository
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
            repository = MetadataRepository(adapter)
            async with session_factory() as session:
                state = ProfileStateRepository(session)
                if force:
                    # Genuinely wipe the triple store (not just the marker) so a
                    # previous profile's graphs don't linger and collide with the
                    # new one — what the confirmation prompt promises.
                    await adapter.clear_all()
                    await state.clear()
                    await session.commit()
                return await apply_profile(
                    profile,
                    repository=repository,
                    state=state,
                    session=session,
                    settings=settings,
                    force=force,
                )
    finally:
        await engine.dispose()


async def _load_applied() -> dict[str, str] | None:
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.profiles.state import ProfileStateRepository
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            state = ProfileStateRepository(session)
            row = await state.current()
            if row is None:
                return None
            return {k: str(v) for k, v in ProfileStateRepository.to_dict(row).items()}
    finally:
        await engine.dispose()


# --- persistent-identifier commands (ADR-0014) ---------------------------


@pid_app.command("w3id-config")
def pid_w3id_config(
    prefix: str = typer.Option(
        None, "--prefix", help="W3ID prefix; derived from IDENTIFIER_BASE if it is a w3id URL."
    ),
) -> None:
    """Print the W3ID redirect .htaccess (+ README) for this deployment.

    No network access, no secrets. Commit the output under ``<prefix>/`` in a
    fork of ``perma-id/w3id.org`` and open a PR — or use ``fdp pid w3id-pr`` to
    do that automatically.
    """
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.pid.w3id import build_w3id_config

    settings = get_settings()
    try:
        config = build_w3id_config(
            identifier_base=settings.resolved_identifier_base,
            serving_base=settings.serving_base,
            prefix=prefix or settings.pid.w3id_prefix,
        )
    except ValueError as err:
        console.print(f"[red]{err}[/]")
        raise typer.Exit(code=1) from err

    console.print(f"[green]W3ID redirect[/] https://w3id.org/{config.prefix} → {config.target}")
    console.print(f"\n[bold]{config.path}[/]")
    console.print(config.htaccess)
    console.print(f"[bold]{config.prefix}/README.md[/]")
    console.print(config.readme)


@pid_app.command("w3id-pr")
def pid_w3id_pr(
    prefix: str = typer.Option(None, "--prefix", help="Override the derived W3ID prefix."),
) -> None:
    """Fork w3id.org and open/update the redirect PR (opt-in; needs a token).

    Requires ``FDP_PID_GITHUB_TOKEN``. Idempotent and reusable: re-run after a
    deployment move to update the redirect target on the existing PR. Every
    request host is checked against ``FDP_PID_ALLOWED_HOSTS``.
    """
    from fdpneo_server.config import get_settings

    settings = get_settings()
    if settings.pid.github_token is None:
        console.print(
            "[yellow]no GitHub token[/] — set FDP_PID_GITHUB_TOKEN to open the w3id.org PR "
            "(or run `fdp pid w3id-config` and submit it manually)."
        )
        raise typer.Exit(code=1)

    try:
        result = asyncio.run(_run_w3id_pr(prefix))
    except Exception as err:
        console.print(f"[red]w3id PR failed:[/] {err}")
        raise typer.Exit(code=1) from err

    verb = "opened" if result.created_pr else "updated"
    console.print(f"[green]w3id PR {verb}[/] {result.pull_request_url} (branch {result.branch})")


@pid_app.command("verify")
def pid_verify(
    iri: list[str] = typer.Option(
        None, "--iri", help="Identifier(s) to resolve. Defaults to the FDP root."
    ),
) -> None:
    """Check that the FDP's persistent identifiers redirect and resolve here.

    Performs real HTTP requests against ``IDENTIFIER_BASE`` IRIs and confirms
    they land on the serving origin with a successful response. In dev (no PID
    base) the redirect is a no-op and this just checks the root resolves.
    """
    try:
        report = asyncio.run(_run_pid_verify(iri or []))
    except Exception as err:
        console.print(f"[red]verification failed:[/] {err}")
        raise typer.Exit(code=1) from err

    for check in report.checks:
        mark = "[green]ok[/]" if check.ok else "[red]FAIL[/]"
        suffix = f" → {check.redirected_to}" if check.redirected_to else ""
        detail = f" ({check.detail})" if check.detail else ""
        console.print(f"  {mark} {check.iri}{suffix}{detail}")
    if not report.ok:
        raise typer.Exit(code=1)
    console.print(
        f"[green]all {len(report.checks)} identifier(s) resolve to {report.serving_base}[/]"
    )


@pid_app.command("rebase")
def pid_rebase(
    from_base: str = typer.Option(..., "--from", help="The old base records currently live under."),
    to_base: str = typer.Option(
        None, "--to", help="The new identifier base. Defaults to IDENTIFIER_BASE."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would move; write nothing."),
) -> None:
    """One-time: move existing record IRIs from an old base to the PID base.

    For a deployment adopting a persistent ``IDENTIFIER_BASE`` after it was
    bootstrapped under ``BASE_URL``. Re-keys every named graph under ``--from``
    to the new base and rewrites cross-record IRIs. Idempotent; after adoption
    the identifier base never changes again (a move only re-points the redirect).
    """
    from fdpneo_server.config import get_settings

    settings = get_settings()
    target = (to_base or settings.resolved_identifier_base).rstrip("/")
    try:
        report = asyncio.run(_run_pid_rebase(from_base.rstrip("/"), target, dry_run))
    except Exception as err:
        console.print(f"[red]rebase failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if report.count == 0:
        console.print("[green]nothing to rebase[/] — no graphs under the old base")
        return
    verb = "would move" if dry_run else "moved"
    console.print(f"[green]{verb}[/] {report.count} graph(s) → {report.new_base}")
    for old, new in report.moved:
        console.print(f"  {old} → {new}")


async def _run_w3id_pr(prefix: str | None) -> PublishResult:
    import httpx

    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.pid.github import W3IDPublisher
    from fdpneo_server.metadata.pid.w3id import build_w3id_config

    settings = get_settings()
    assert settings.pid.github_token is not None  # guarded by the caller
    config = build_w3id_config(
        identifier_base=settings.resolved_identifier_base,
        serving_base=settings.serving_base,
        prefix=prefix or settings.pid.w3id_prefix,
    )
    async with httpx.AsyncClient(timeout=settings.pid.timeout_seconds) as http_client:
        publisher = W3IDPublisher(
            http_client=http_client,
            token=settings.pid.github_token.get_secret_value(),
            allowed_hosts=settings.pid.allowed_hosts,
            fork_owner=settings.pid.github_fork_owner,
        )
        return await publisher.publish(config)


async def _run_pid_verify(iris: list[str]) -> ResolutionReport:
    import httpx

    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.pid.verify import verify_resolution

    settings = get_settings()
    targets = iris or [settings.resolved_identifier_base]
    async with httpx.AsyncClient() as http_client:
        return await verify_resolution(
            identifier_base=settings.resolved_identifier_base,
            serving_base=settings.serving_base,
            iris=targets,
            http_client=http_client,
            timeout_seconds=settings.pid.timeout_seconds,
        )


async def _run_pid_rebase(from_base: str, to_base: str, dry_run: bool) -> RebaseReport:
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.pid.rebase import rebase_identifiers
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
        return await rebase_identifiers(
            adapter=adapter, old_base=from_base, new_base=to_base, dry_run=dry_run
        )


# --- backup / restore / migration commands (ADR-0016) --------------------


@backup_app.command("dump")
def backup_dump(
    path: Path = typer.Argument(..., file_okay=False),
    no_audit: bool = typer.Option(
        False, "--no-audit", help="Skip exporting the Postgres record_audit rows."
    ),
) -> None:
    """Dump every named graph (+ manifest, + audit) to a directory (ADR-0016 §2).

    Storage-level and faithful: reads through the adapter, not the LDP layer, so
    provenance, the ADR-0019 record-schema binding, and audit graphs survive
    byte-for-byte. Writes ``records.nq``, ``manifest.json`` and (unless
    ``--no-audit``) ``audit.jsonl``. Whole-store only in v1.
    """
    try:
        result = asyncio.run(_run_dump(path, include_audit=not no_audit))
    except Exception as err:
        console.print(f"[red]dump failed:[/] {err}")
        raise typer.Exit(code=1) from err

    console.print(
        f"[green]dumped[/] {result.graph_count} graph(s), {result.quad_count} quad(s), "
        f"{result.audit_rows} audit row(s) → {result.out_dir} "
        f"(data model: {result.data_model_version})"
    )


@backup_app.command("restore")
def backup_restore(
    path: Path = typer.Argument(..., exists=True, file_okay=False),
    merge: bool = typer.Option(
        False, "--merge", help="Into a non-empty store: skip graphs that already exist."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Into a non-empty store: replace existing graphs."
    ),
    no_audit: bool = typer.Option(
        False, "--no-audit", help="Skip inserting the dump's audit.jsonl rows."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change; write nothing."
    ),
) -> None:
    """Faithfully restore a dump into the triple store (ADR-0016 §3).

    Loads quads verbatim (no re-stamped provenance). Refuses on an identifier_base
    mismatch (use `fdp backup import --rebase`) or a non-empty store (unless
    --merge/--overwrite). Afterwards it inserts audit rows, migrates a pre-ADR-0019
    dump forward, and reindexes search.
    """
    if merge and overwrite:
        console.print("[red]--merge and --overwrite are mutually exclusive[/]")
        raise typer.Exit(code=1)
    try:
        result, audit_rows, profiles, indexed = asyncio.run(
            _run_restore(
                path, merge=merge, overwrite=overwrite, include_audit=not no_audit, dry_run=dry_run
            )
        )
    except Exception as err:
        console.print(f"[red]restore failed:[/] {err}")
        raise typer.Exit(code=1) from err

    verb = "would restore" if result.dry_run else "restored"
    console.print(
        f"[green]{verb}[/] {result.graphs_loaded} graph(s), {result.quad_count} quad(s) "
        f"(skipped {result.graphs_skipped}) from {path}"
    )
    if not result.dry_run:
        if result.needs_migration:
            console.print(
                f"  migrated pre-ADR-0019 dump forward: {profiles} profile(s) provisioned"
            )
        console.print(f"  inserted {audit_rows} audit row(s); reindexed {indexed} record(s)")


async def _run_restore(
    in_dir: Path,
    *,
    merge: bool,
    overwrite: bool,
    include_audit: bool,
    dry_run: bool,
    rebase: bool = False,
) -> tuple[RestoreResult, int, int, int]:
    """Load a dump, then (unless dry-run) insert audit, migrate, and reindex.

    Returns (restore result, audit rows inserted, profiles provisioned, records indexed).
    """
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.backup import orchestrate_restore
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
            outcome = await orchestrate_restore(
                adapter,
                session_factory,
                settings=settings,
                in_dir=in_dir,
                merge=merge,
                overwrite=overwrite,
                include_audit=include_audit,
                dry_run=dry_run,
                rebase=rebase,
            )
        return (
            outcome.result,
            outcome.audit_rows,
            outcome.profiles_provisioned,
            outcome.records_indexed,
        )
    finally:
        await engine.dispose()


@backup_app.command("import")
def backup_import(
    path: Path | None = typer.Argument(None, file_okay=False),
    from_url: str = typer.Option(
        None,
        "--from",
        help="Crawl a reference-FDP instance at this base URL and import its records (18.5).",
    ),
    rebase: bool = typer.Option(
        False,
        "--rebase",
        help="Adopt an FDPneo dump (positional dir) captured under a different identifier_base.",
    ),
    merge: bool = typer.Option(
        False, "--merge", help="Into a non-empty store: skip graphs that already exist."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Into a non-empty store: replace existing graphs."
    ),
    no_audit: bool = typer.Option(
        False, "--no-audit", help="Skip inserting the dump's audit.jsonl rows (--rebase only)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change; write nothing."
    ),
) -> None:
    """Import records into this FDP, adopting them under this deployment's base (ADR-0016 §4).

    Two modes:
    - `--from <url>`: crawl a reference-FDP instance's LDP tree and import its records
      (re-rooted to this base, source provenance carried, old IRI preserved as an
      alternative identifier), then bind them to this deployment's profiles.
    - `<dir> --rebase`: adopt an FDPneo dump captured under a different identifier_base.
    """
    if from_url:
        _import_reference(from_url, dry_run=dry_run)
        return
    if not rebase:
        console.print(
            "[red]specify a mode[/] — `--from <url>` to crawl a reference FDP, or "
            "`<dir> --rebase` to adopt an FDPneo dump. For a same-base dump use "
            "`fdp backup restore`."
        )
        raise typer.Exit(code=1)
    if path is None:
        console.print("[red]--rebase needs a dump directory argument[/]")
        raise typer.Exit(code=1)
    if merge and overwrite:
        console.print("[red]--merge and --overwrite are mutually exclusive[/]")
        raise typer.Exit(code=1)
    try:
        result, audit_rows, profiles, indexed = asyncio.run(
            _run_restore(
                path,
                merge=merge,
                overwrite=overwrite,
                include_audit=not no_audit,
                dry_run=dry_run,
                rebase=True,
            )
        )
    except Exception as err:
        console.print(f"[red]import failed:[/] {err}")
        raise typer.Exit(code=1) from err

    verb = "would import" if result.dry_run else "imported"
    console.print(
        f"[green]{verb} (rebased)[/] {result.graphs_loaded} graph(s), {result.quad_count} quad(s) "
        f"(skipped {result.graphs_skipped}) → base {result.identifier_base}"
    )
    if not result.dry_run:
        if result.needs_migration:
            console.print(
                f"  migrated pre-ADR-0019 dump forward: {profiles} profile(s) provisioned"
            )
        console.print(f"  inserted {audit_rows} audit row(s); reindexed {indexed} record(s)")


def _import_reference(from_url: str, *, dry_run: bool) -> None:
    """Crawl a reference FDP and import its records (18.5 / ADR-0016 §4)."""
    try:
        report, profiles, indexed = asyncio.run(_run_import_reference(from_url, dry_run=dry_run))
    except Exception as err:
        console.print(f"[red]import failed:[/] {err}")
        raise typer.Exit(code=1) from err

    verb = "would import" if report.dry_run else "imported"
    console.print(
        f"[green]{verb}[/] {report.count} record(s) from {report.source_base} "
        f"→ base {report.target_base} (skipped {len(report.skipped)}"
        f"{', truncated' if report.truncated else ''})"
    )
    if report.validation_issues:
        console.print(
            f"  [yellow]{len(report.validation_issues)} validation issue(s) (report-only)[/]"
        )
    if not report.dry_run:
        console.print(f"  bound {profiles} profile(s); reindexed {indexed} record(s)")


async def _run_import_reference(from_url: str, *, dry_run: bool) -> tuple[ImportReport, int, int]:
    """Crawl + import a reference FDP, then bind to our profiles and reindex."""
    import httpx

    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.backup import import_reference_fdp
    from fdpneo_server.metadata.prof_backfill import backfill_conformance
    from fdpneo_server.metadata.profiles import build_cache_from_repository
    from fdpneo_server.metadata.repository import MetadataRepository
    from fdpneo_server.metadata.search.reindex import reindex_all
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    profiles = 0
    indexed = 0
    try:
        async with (
            TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter,
            httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client,
        ):
            repository = MetadataRepository(adapter)
            report = await import_reference_fdp(
                repository=repository,
                http_client=http_client,
                source_base=from_url,
                target_base=settings.resolved_identifier_base,
                dry_run=dry_run,
            )
            if not dry_run and report.count:
                # Bind the imported records to THIS deployment's profiles
                # (conformsTo / validatedAgainst) and rebuild the search index.
                cache = await build_cache_from_repository(
                    adapter, base_url=settings.resolved_identifier_base
                )
                backfill = await backfill_conformance(
                    adapter=adapter, repository=repository, cache=cache
                )
                profiles = len(backfill.profiles_provisioned)
                indexed = await reindex_all(
                    adapter,
                    session_factory,
                    language=settings.search.default_language,
                    system_default_offer_iri=_system_default_offer(settings),
                )
        return report, profiles, indexed
    finally:
        await engine.dispose()


async def _run_dump(out_dir: Path, *, include_audit: bool) -> DumpResult:
    """Build the runtime collaborators and dump the store."""
    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.backup import dump_store
    from fdpneo_server.storage.postgres.engine import build_engine, build_session_factory
    from fdpneo_server.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with TripleStoreAdapter.from_settings(settings.require_triplestore()) as adapter:
            return await dump_store(
                adapter,
                out_dir,
                identifier_base=settings.resolved_identifier_base,
                session_factory=session_factory if include_audit else None,
                include_audit=include_audit,
            )
    finally:
        await engine.dispose()


# --- index / discovery commands (ADR-0020/0021) --------------------------


@index_app.command("ping")
def index_ping() -> None:
    """Announce this FDP to the configured indexes once (task 8.1).

    POSTs ``{"clientUrl": <base>}`` to every ``FDP_INDEX_PING_TARGETS`` entry
    (reference wire protocol; 204 = accepted, 429 = rate-limited). For an external
    scheduler; the server also pings in-process unless ``FDP_INDEX_PING_IN_PROCESS``
    is false.
    """
    try:
        results = asyncio.run(_run_index_ping())
    except Exception as err:
        console.print(f"[red]index ping failed:[/] {err}")
        raise typer.Exit(code=1) from err

    if not results:
        console.print("[yellow]no index targets configured[/] (set FDP_INDEX_PING_TARGETS)")
        raise typer.Exit(code=0)
    for result in results:
        mark = "[green]ok[/]" if result.ok else "[red]FAIL[/]"
        detail = f" ({result.detail})" if result.detail else ""
        console.print(f"  {mark} {result.target}{detail}")
    if any(not result.ok for result in results):
        raise typer.Exit(code=1)


async def _run_index_ping() -> list[PingResult]:
    """Ping every configured index once; returns per-target results."""
    import httpx

    from fdpneo_server.config import get_settings
    from fdpneo_server.metadata.index_ping import ping_indexes

    settings = get_settings()
    if not settings.index.enabled:
        return []
    client_url = settings.index.ping_client_url.strip() or settings.resolved_identifier_base.rstrip(
        "/"
    )
    async with httpx.AsyncClient() as http_client:
        return await ping_indexes(
            http_client,
            client_url=client_url,
            targets=settings.index.targets,
            timeout_seconds=settings.index.ping_timeout_seconds,
        )


if __name__ == "__main__":
    app()
