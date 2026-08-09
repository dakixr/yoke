"""main module."""

from __future__ import annotations

import sys
from typing import Any
from typing import TYPE_CHECKING
from typing import cast

import click
import typer

from yoke.cli.main_core import _inject_prompt_flag
from yoke.cli.main_core import _load_source_dotenv
from yoke.cli.main_core import build_cli_args
from yoke.cli.main_commands import register_commands

if TYPE_CHECKING:
    from yoke.cli.config import CLIArgs
    from yoke.cli.config import build_agent_from_args
    from yoke.cli.interactive import PromptToolkitLiveRenderer
    from yoke.cli.interactive import run_prompt_toolkit_cli
    from yoke.cli.runtime import run_cli
    from yoke.cli.runtime import run_resume_cli

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
    _load_source_dotenv()
    argv = _inject_prompt_flag(list(argv) if argv is not None else sys.argv[1:])
    try:
        result = app(args=argv, prog_name="yoke", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    except typer.Exit as exc:
        return int(exc.exit_code)
    except Exception as exc:
        if exc.__class__.__name__ == "UsageError" and hasattr(exc, "show"):
            cast(Any, exc).show()
            return int(getattr(exc, "exit_code", 2))
        raise
    if isinstance(result, int):
        return result
    return 0


_LAZY_EXPORT_MODULES = {
    "CLIArgs": "yoke.cli.config",
    "build_agent_from_args": "yoke.cli.config",
    "PromptToolkitLiveRenderer": "yoke.cli.interactive",
    "run_prompt_toolkit_cli": "yoke.cli.interactive",
    "run_cli": "yoke.cli.runtime",
    "run_resume_cli": "yoke.cli.runtime",
}


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Load backward-compatible CLI exports on demand."""
    module_name = _LAZY_EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "CLIArgs",
    "PromptToolkitLiveRenderer",
    "app",
    "build_agent_from_args",
    "build_cli_args",
    "main",
    "run_cli",
    "run_prompt_toolkit_cli",
    "run_resume_cli",
]
