"""Register utility commands that do not run an agent conversation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yoke.cli.main_core import CWD


def register_utility_commands(app: typer.Typer) -> None:
    """Attach version and MCP inspection commands."""

    @app.command()
    def version() -> None:
        """Print the yoke version and exit."""
        import click

        from yoke._version import __version__

        click.echo(__version__)

    @app.command("mcp")
    def mcp_command(
        server: Annotated[
            str | None,
            typer.Argument(help="Optional MCP server name to inspect."),
        ] = None,
        root: Annotated[
            Path,
            typer.Option(
                "--root",
                help="Workspace root used for .yoke/mcp.json and MCP roots/list.",
                file_okay=False,
                dir_okay=True,
                resolve_path=True,
            ),
        ] = CWD,
    ) -> None:
        """Show configured MCP servers and compact tool lists."""
        import click

        from yoke.cli.mcp_app import format_mcp_status

        click.echo(format_mcp_status(root=root, home=Path.home(), server=server))
