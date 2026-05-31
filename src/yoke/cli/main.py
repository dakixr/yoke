"""main module."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import cast

import click
import typer

from yoke import __version__
from yoke.cli.config import CLIArgs
from yoke.cli.config import build_agent_from_args
from yoke.cli.interactive import PromptToolkitLiveRenderer
from yoke.cli.interactive import run_prompt_toolkit_cli
from yoke.cli.models_app import models_app
from yoke.cli.providers.app import providers_app
from yoke.cli.providers.app import providers_login
from yoke.cli.runtime import run_cli
from yoke.cli.runtime import run_resume_cli
from yoke.cli.skills_app import skills_app
from yoke.cli.tools.app import tools_app

CWD = Path.cwd().absolute()
SOURCE_ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(
    add_completion=False,
    help="Native Python coding agent CLI.",
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(tools_app, name="tools")
app.add_typer(models_app, name="models")
app.add_typer(providers_app, name="providers")
app.add_typer(skills_app, name="skills")


@app.command()
def version() -> None:
    """Print the yoke version and exit."""
    click.echo(__version__)


def build_cli_args(
    *,
    prompt: str | None = None,
    headless: bool = False,
    session: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    root: Path,
    skills: tuple[str, ...] = (),
    images: tuple[str, ...] = (),
) -> CLIArgs:
    return CLIArgs(
        prompt=prompt,
        headless=headless,
        session=session,
        model=model,
        reasoning_effort=reasoning_effort,
        root=str(root),
        skills=skills,
        images=images,
    )


@app.callback()
def cli(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", help="Prompt to seed the session with."),
    ] = None,
    headless: Annotated[
        bool,
        typer.Option(
            "--headless",
            help=(
                "Run one non-interactive prompt and exit. Requires --prompt "
                "or piped stdin."
            ),
        ),
    ] = False,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help=(
                "Persist conversation under [bold].yoke/sessions/<name>.json[/bold]."
            ),
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Model to send to the provider. Use `provider-name:model-name` "
                "to select a specific provider, or just `model-name` to "
                "let yoke pick a provider from available credentials."
            ),
        ),
    ] = None,
    reasoning_effort: Annotated[
        str | None,
        typer.Option(
            "--reasoning-effort",
            help=(
                "Reasoning effort for supported chat-completions models: "
                "none, low, medium, high, or xhigh."
            ),
        ),
    ] = None,
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root for tools.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = CWD,
    skill: Annotated[
        list[str] | None,
        typer.Option("--skill", help="Preload a skill by name."),
    ] = None,
    image: Annotated[
        list[str] | None,
        typer.Option(
            "--image",
            help=(
                "Attach a local image to the initial prompt. "
                "Repeat for multiple images."
            ),
        ),
    ] = None,
) -> None:
    skill = [] if skill is None else skill
    image = [] if image is None else image
    if ctx.invoked_subcommand is not None:
        return
    raise typer.Exit(
        run_cli(
            build_cli_args(
                prompt=prompt,
                headless=headless,
                session=session,
                model=model,
                reasoning_effort=reasoning_effort,
                root=root,
                skills=tuple(skill),
                images=tuple(image),
            )
        )
    )


@app.command()
def login(
    name: Annotated[
        str,
        typer.Argument(help="Provider name to login to."),
    ],
) -> None:
    """Interactively store credentials for a provider."""
    providers_login(name)


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
            help=("Show sessions from all workspace roots when choosing a session."),
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Model to send to the provider. Use `provider-name:model-name` "
                "to override the resumed provider as well."
            ),
        ),
    ] = None,
    reasoning_effort: Annotated[
        str | None,
        typer.Option(
            "--reasoning-effort",
            help=(
                "Reasoning effort for supported chat-completions models: "
                "none, low, medium, high, or xhigh."
            ),
        ),
    ] = None,
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root for filtering/resuming sessions.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = CWD,
) -> None:
    raise typer.Exit(
        run_resume_cli(
            build_cli_args(
                model=model,
                reasoning_effort=reasoning_effort,
                root=root,
            ),
            session_id,
            all_sessions=all_sessions,
        )
    )


_SUBCOMMANDS = frozenset(
    {"version", "login", "resume", "tools", "models", "providers", "skills"}
)
_OPTIONS_WITH_VALUES = frozenset(
    {
        "--prompt",
        "--session",
        "--model",
        "--reasoning-effort",
        "--root",
        "--skill",
        "--image",
    }
)


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _load_source_dotenv(source_root: Path = SOURCE_ROOT) -> None:
    """Load the source `.env` into the current process env."""
    dotenv_path = source_root / ".env"
    if not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue
        os.environ[env_key] = _strip_matching_quotes(value.strip())


def _inject_prompt_flag(argv: list[str]) -> list[str]:
    """Convert `yoke "message"` → `yoke --prompt "message"` at the entry point."""
    result = list(argv)
    i = 0
    while i < len(result):
        arg = result[i]
        if arg == "--":
            break
        if arg.startswith("-"):
            i += 1
            if "=" not in arg and arg in _OPTIONS_WITH_VALUES:
                i += 1  # skip option value
        else:
            if arg not in _SUBCOMMANDS:
                result.insert(i, "--prompt")
            return result
    return result


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
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


__all__ = [
    "CLIArgs",
    "PromptToolkitLiveRenderer",
    "app",
    "build_agent_from_args",
    "main",
    "run_cli",
    "run_prompt_toolkit_cli",
    "run_resume_cli",
]
