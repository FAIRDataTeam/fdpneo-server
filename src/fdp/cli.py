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

app = typer.Typer(
    name="fdp",
    help="FAIR Data Point administrative CLI",
    no_args_is_help=True,
    add_completion=False,
)

profile_app = typer.Typer(help="Deployment-profile commands.", no_args_is_help=True)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)

app.add_typer(profile_app, name="profile")
app.add_typer(db_app, name="db")

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

    console.print(
        f"[green]applied[/] {profile.name} {profile.version} — "
        f"{len(report.schemas_written)} schemas, "
        f"{len(report.offers_written)} offers, "
        f"{len(report.containers_written)} containers, "
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
    console.print(
        "[yellow]profile export is deferred to v1.x[/] "
        f"(would have written to {path})"
    )
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
