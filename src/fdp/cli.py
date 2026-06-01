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

The CLI is built with Typer for argument parsing and Rich for output.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from fdp.metadata.profiles.applier import ApplyReport
    from fdp.metadata.profiles.manifest import DeploymentProfile
    from fdp.metadata.schema_sync import SyncReport
    from fdp.metrics.aggregation import RollupResult

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

app.add_typer(profile_app, name="profile")
app.add_typer(db_app, name="db")
app.add_typer(metrics_app, name="metrics")
app.add_typer(schema_app, name="schema")

console = Console()


# --- profile commands ----------------------------------------------------


@profile_app.command("validate")
def profile_validate(
    path: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    """Validate a profile bundle without applying it."""
    from fdp.metadata.profiles import load_profile, validate_profile
    from fdp.shared.errors import FDPError

    try:
        profile = load_profile(path)
    except FDPError as err:
        console.print(f"[red]profile load failed:[/] {err.message}")
        raise typer.Exit(code=1) from err

    report = validate_profile(profile)
    if report.ok:
        console.print(
            f"[green]ok[/] {profile.name} {profile.version} — "
            f"{len(profile.schemas)} schemas, "
            f"{len(profile.offers)} offers, "
            f"{len(profile.manifest.containers)} containers, "
            f"{len(profile.seed_records)} seed records"
        )
        return

    console.print(f"[red]{len(report.issues)} validation issue(s):[/]")
    for issue in report.issues:
        console.print(f"  [yellow]{issue.where}[/] ({issue.code}): {issue.message}")
    raise typer.Exit(code=1)


@profile_app.command("apply")
def profile_apply(
    path: Path = typer.Argument(..., exists=True, file_okay=False),
    force: bool = typer.Option(
        False,
        "--force",
        help="Wipe the existing applied profile (including ALL named graphs) before applying.",
    ),
) -> None:
    """Apply a profile to bootstrap an uninitialized FDP."""
    from fdp.metadata.profiles import load_profile
    from fdp.shared.errors import FDPError

    try:
        profile = load_profile(path)
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
    from fdp.shared.errors import FDPError

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


@db_app.command("migrate")
def db_migrate() -> None:
    """Run database migrations to the latest revision."""
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    command.upgrade(config, "head")
    console.print("[green]migrations applied to head[/]")


# --- metrics commands ----------------------------------------------------


@metrics_app.command("rollup")
def metrics_rollup(
    hourly_only: bool = typer.Option(False, "--hourly-only", help="Run raw → hourly only."),
    daily_only: bool = typer.Option(False, "--daily-only", help="Run hourly → daily only."),
) -> None:
    """Run the metrics rollups once.

    The aggregation logic is the same as ``fdp.metrics.aggregation`` — we
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
    from fdp.config import get_settings

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


async def _run_schema_sync() -> SyncReport:
    """Build the runtime collaborators and run one sync pass."""
    import httpx

    from fdp.config import get_settings
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.schema_sync import SchemaSyncService
    from fdp.metadata.schemas import SchemaService
    from fdp.metadata.shacl import ShaclValidator
    from fdp.metadata.shape_provider import MetadataShapeProvider
    from fdp.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    async with (
        TripleStoreAdapter.from_settings(settings.triplestore) as adapter,
        httpx.AsyncClient() as http_client,
    ):
        repository = MetadataRepository(adapter)
        schema_service = SchemaService(
            repository=repository,
            adapter=adapter,
            validator=ShaclValidator(MetadataShapeProvider(repository)),
            base_url=str(settings.base_url),
        )
        syncer = SchemaSyncService(
            schema_service=schema_service,
            adapter=adapter,
            http_client=http_client,
            settings=settings.schema_sync,
            base_url=str(settings.base_url),
        )
        return await syncer.sync_all()


async def _run_rollup(
    *,
    hourly_only: bool,
    daily_only: bool,
) -> tuple[RollupResult | None, RollupResult | None]:
    """Drive ``aggregation`` once. Returns (raw_result, daily_result)."""
    from fdp.config import get_settings
    from fdp.metrics.aggregation import (
        roll_up_hourly_to_daily,
        roll_up_raw_to_hourly,
    )
    from fdp.storage.postgres.engine import build_engine, build_session_factory

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
    from fdp.config import get_settings
    from fdp.metadata.profiles.applier import apply_profile
    from fdp.metadata.profiles.state import ProfileStateRepository
    from fdp.metadata.repository import MetadataRepository
    from fdp.storage.postgres.engine import build_engine, build_session_factory
    from fdp.storage.triplestore.adapter import TripleStoreAdapter

    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with TripleStoreAdapter.from_settings(settings.triplestore) as adapter:
            repository = MetadataRepository(adapter)
            async with session_factory() as session:
                state = ProfileStateRepository(session)
                if force:
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
    from fdp.config import get_settings
    from fdp.metadata.profiles.state import ProfileStateRepository
    from fdp.storage.postgres.engine import build_engine, build_session_factory

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


if __name__ == "__main__":
    app()
