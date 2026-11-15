"""`yoke db`: inspect, back up and import into the Yoke database."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer

db_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Inspect, back up, and import into the Yoke session database.",
)


def _store():  # noqa: ANN202
    from yoke.session import SessionStore

    return SessionStore()


@db_app.command("info")
def info() -> None:
    """Print the database path, schema version, sizes and row counts."""
    from yoke.storage.admin import database_info

    for line in database_info(_store().database).lines():
        typer.echo(line)


@db_app.command("backup")
def backup(
    path: Annotated[Path, typer.Argument(help="New file to write the copy to.")],
) -> None:
    """Write a compact, consistent copy of the database (VACUUM INTO)."""
    from yoke.storage.admin import backup_database

    try:
        target = backup_database(_store().database, path)
    except FileExistsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Backed up to {target}")


@db_app.command("import-legacy")
def import_legacy(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Session id to import. Omit with --all."),
    ] = None,
    all_sessions: Annotated[
        bool, typer.Option("--all", help="Import every session in the directory.")
    ] = False,
    source: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="Legacy JSONL session directory (default: ~/.yoke/sessions).",
        ),
    ] = None,
) -> None:
    """Import JSONL sessions from before the SQLite store, verifying each one.

    Safe to rerun: sessions already in the database are skipped.
    """
    from yoke.session.legacy.importer import import_legacy_sessions

    if (session_id is None) == (not all_sessions):
        typer.echo("Error: pass a SESSION_ID or --all (not both).", err=True)
        raise typer.Exit(2)
    directory = (source or Path.home() / ".yoke" / "sessions").expanduser()
    if not directory.is_dir():
        typer.echo(f"Error: {directory} is not a directory.", err=True)
        raise typer.Exit(2)
    store = _store()
    counts = {"imported": 0, "skipped": 0, "mismatch": 0, "failed": 0}
    started = time.monotonic()
    ids = None if all_sessions else [session_id or ""]
    for result in import_legacy_sessions(store, directory, ids):
        counts[result.status] += 1
        if result.status in {"mismatch", "failed"}:
            typer.echo(
                f"{result.status}: {result.session_id}: {result.detail}", err=True
            )
        elif not all_sessions:
            typer.echo(
                f"{result.status}: {result.session_id} ({result.entries} entries)"
            )
    elapsed = time.monotonic() - started
    typer.echo(
        "Imported {imported}, skipped {skipped}, mismatched {mismatch}, "
        "failed {failed}".format(**counts)
        + f" in {elapsed:.1f}s into {store.database_path}"
    )
    if counts["mismatch"] or counts["failed"]:
        raise typer.Exit(1)
