"""main module."""

from __future__ import annotations

import sys

import click
import typer

from yoke.cli.main_core import _inject_prompt_flag
from yoke.cli.main_core import _load_source_dotenv
from yoke.cli.main_core import build_cli_args
from yoke.cli.main_commands import register_commands

app = typer.Typer(
    add_completion=False,
    help="Native Python coding agent CLI.",
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)

register_commands(app)


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    argv = _inject_prompt_flag(list(argv) if argv is not None else sys.argv[1:])
    if argv[:1] != ["serve"]:
        _load_source_dotenv()
    try:
        result = app(args=argv, prog_name="yoke", standalone_mode=False)
    except click.UsageError as exc:
        exc.show()
        return int(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    except typer.Exit as exc:
        return int(exc.exit_code)
    if isinstance(result, int):
        return result
    return 0


__all__ = ["app", "build_cli_args", "main"]
