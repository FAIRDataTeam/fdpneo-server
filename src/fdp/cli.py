"""FDP command-line interface.

Exposes administrative commands for managing a deployment: bootstrap with
a deployment profile, inspect applied state, and export current state as a
distributable profile.

Subcommands:

* ``fdp profile validate <path>`` — dry-run validation of a profile bundle.
* ``fdp profile apply <path>``    — bootstrap (refuses if already initialized).
* ``fdp profile info``            — show the applied profile name and version.
* ``fdp profile export <path>``   — serialize current state to a profile.
* ``fdp db migrate``              — run Alembic migrations.

The CLI is built with Typer for argument parsing and Rich for output.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

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


@profile_app.command("validate")
def profile_validate(path: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    """Validate a profile bundle without applying it."""
    # TODO: implement against fdp.metadata.profiles
    console.print(f"[yellow]TODO[/] validate profile at {path}")
    raise typer.Exit(code=1)


@profile_app.command("apply")
def profile_apply(path: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    """Apply a profile to bootstrap an uninitialized FDP."""
    # TODO: implement against fdp.metadata.profiles
    console.print(f"[yellow]TODO[/] apply profile at {path}")
    raise typer.Exit(code=1)


@profile_app.command("info")
def profile_info() -> None:
    """Show the applied profile name and version."""
    # TODO: query Postgres for the profile-applied marker
    console.print("[yellow]TODO[/] show applied profile info")
    raise typer.Exit(code=1)


@profile_app.command("export")
def profile_export(path: Path = typer.Argument(..., file_okay=False)) -> None:
    """Export the current FDP state as a distributable profile."""
    # TODO: implement export of schemas, offers, container hierarchy, seed records
    console.print(f"[yellow]TODO[/] export profile to {path}")
    raise typer.Exit(code=1)


@db_app.command("migrate")
def db_migrate() -> None:
    """Run database migrations to the latest revision."""
    # TODO: shell to Alembic
    console.print("[yellow]TODO[/] run alembic upgrade head")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
