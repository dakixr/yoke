"""Explicit session resume and workspace relocation options."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yoke.cli.main_core import CWD, build_cli_args


def register_resume_command(app: typer.Typer) -> None:
    """Register resume without importing the agent runtime during CLI startup."""

    @app.command()
    def resume(
        session_id: Annotated[
            str | None,
            typer.Argument(help="Session id to resume. Omit to choose from this root."),
        ] = None,
        all_sessions: Annotated[
            bool,
            typer.Option(
                "--all",
                help="Show sessions from all workspace roots when choosing a session.",
            ),
        ] = False,
        model: Annotated[
            str | None,
            typer.Option(
                "--model",
                help=(
                    "Model selection as `provider-name:model-name[:thinking-effort]`. "
                    "Including the provider overrides the resumed provider as well."
                ),
            ),
        ] = None,
        root: Annotated[
            Path,
            typer.Option(
                "--root",
                help=(
                    "Filter the session chooser by workspace root. "
                    "Does not relocate saved sessions."
                ),
                file_okay=False,
                dir_okay=True,
                resolve_path=True,
            ),
        ] = CWD,
        relocate: Annotated[
            Path | None,
            typer.Option(
                "--relocate",
                help=(
                    "Explicitly move the saved workspace binding. "
                    "Requires a session ID."
                ),
            ),
        ] = None,
    ) -> None:
        from yoke.cli.runtime import run_resume_cli

        raise typer.Exit(
            run_resume_cli(
                build_cli_args(model=model, root=root),
                session_id,
                all_sessions=all_sessions,
                relocate=relocate,
            )
        )
