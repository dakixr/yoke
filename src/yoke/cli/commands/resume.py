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
        headless: Annotated[
            bool,
            typer.Option(
                "--headless",
                help="Run one prompt against the saved session and exit.",
            ),
        ] = False,
        prompt: Annotated[
            str | None,
            typer.Option("--prompt", "-p", help="Prompt for a headless resumed turn."),
        ] = None,
        image: Annotated[
            list[str] | None,
            typer.Option(
                "--image", help="Attach a local image to the resumed headless prompt."
            ),
        ] = None,
    ) -> None:
        if headless or prompt is not None:
            from yoke.cli.backend.startup import run_daemon_headless

            if session_id is None:
                import click

                click.echo("Error: Headless resume requires a session ID.", err=True)
                raise typer.Exit(1)
            raise typer.Exit(
                run_daemon_headless(
                    build_cli_args(
                        model=model,
                        root=root,
                        prompt=prompt,
                        headless=True,
                        images=tuple(image or ()),
                    ),
                    resume_id=session_id,
                    relocate=relocate,
                )
            )
        if session_id is None:
            from yoke.cli.backend.startup import run_daemon_interactive

            raise typer.Exit(
                run_daemon_interactive(
                    build_cli_args(model=model, root=root),
                    picker=True,
                    all_sessions=all_sessions,
                )
            )
        from yoke.cli.backend.startup import run_daemon_interactive

        raise typer.Exit(
            run_daemon_interactive(
                build_cli_args(model=model, root=root),
                resume_id=session_id,
                relocate=relocate,
            )
        )
