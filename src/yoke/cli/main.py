"""main module."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import TYPE_CHECKING
from typing import cast

import click
import typer

from yoke._version import __version__
from yoke.cli.config.args import CLIArgs

if TYPE_CHECKING:
    from yoke.cli.config import build_agent_from_args as build_agent_from_args
    from yoke.cli.interactive import PromptToolkitLiveRenderer
    from yoke.cli.interactive import run_prompt_toolkit_cli as run_prompt_toolkit_cli
    from yoke.cli.runtime import run_continue_cli as run_continue_cli
    from yoke.cli.runtime import run_cli as run_cli
    from yoke.cli.runtime import run_resume_cli as run_resume_cli

CWD = Path.cwd().absolute()
SOURCE_ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(
    add_completion=False,
    help="Native Python coding agent CLI.",
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)


tools_app = typer.Typer(help="Manage dynamically loaded tools.")
models_app = typer.Typer(
    help="Inspect available models and configure the default model."
)
providers_app = typer.Typer(help="Manage dynamically loaded providers.")
observe_app = typer.Typer(help="Inspect observed SDK workflow runs.")
skills_app = typer.Typer(
    help=(
        "Manage skills. The CLI discovers built-in skills from the yoke "
        "codebase plus ~/.yoke/skills and <repo>/.yoke/skills by default."
    )
)
app.add_typer(tools_app, name="tools")
app.add_typer(models_app, name="models")
app.add_typer(providers_app, name="providers")
app.add_typer(observe_app, name="observe")
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
    fork_session_id: str | None = None,
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
        fork_session_id=fork_session_id,
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
        typer.Option("--prompt", "-p", help="Prompt to seed the session with."),
    ] = None,
    headless: Annotated[
        bool,
        typer.Option(
            "--headless",
            "-h",
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
            "-s",
            help=(
                "Persist conversation under [bold].yoke/sessions/<name>.jsonl[/bold]."
            ),
        ),
    ] = None,
    fork_session_id: Annotated[
        str | None,
        typer.Option(
            "--fork",
            help="Start by forking an existing session id into a new persisted session.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
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
    if session is not None and fork_session_id is not None:
        click.echo("Error: --fork cannot be used with --session.", err=True)
        raise typer.Exit(1)
    from yoke.cli.runtime import run_cli

    raise typer.Exit(
        run_cli(
            build_cli_args(
                prompt=prompt,
                headless=headless,
                session=session,
                fork_session_id=fork_session_id,
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
    from yoke.cli.providers.app import providers_login

    providers_login(name)


@app.command()
def resume(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Session id to resume. Omit to choose from this root."),
    ] = None,
    explicit_session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help=(
                "Session id to resume. Use this to resume a session whose id "
                "matches a reserved resume action such as 'list'."
            ),
        ),
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
    from yoke.cli.runtime import run_resume_cli

    if session_id is not None and explicit_session_id is not None:
        click.echo("Error: pass either SESSION_ID or --session-id, not both.", err=True)
        raise typer.Exit(1)

    raise typer.Exit(
        run_resume_cli(
            build_cli_args(
                model=model,
                reasoning_effort=reasoning_effort,
                root=root,
            ),
            explicit_session_id or session_id,
            all_sessions=all_sessions,
            allow_reserved_actions=explicit_session_id is None,
        )
    )


@app.command("continue")
def continue_command(
    global_sessions: Annotated[
        bool,
        typer.Option(
            "--global",
            "-g",
            help="Resume the most recent session across all workspace roots.",
        ),
    ] = False,
    fork_session_id: Annotated[
        str | None,
        typer.Option(
            "--fork",
            help="Fork the given session id and continue in the new session.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Model to send to the provider. Use `provider-name:model-name` "
                "to override the continued provider as well."
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
            help="Workspace root for selecting sessions.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = CWD,
) -> None:
    """Resume the most recent saved session for this workspace."""
    from yoke.cli.runtime import run_continue_cli

    raise typer.Exit(
        run_continue_cli(
            build_cli_args(
                model=model,
                reasoning_effort=reasoning_effort,
                root=root,
            ),
            all_sessions=global_sessions,
            fork_session_id=fork_session_id,
        )
    )


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
    from yoke.cli.mcp_app import format_mcp_status

    click.echo(format_mcp_status(root=root, home=Path.home(), server=server))


_SUBCOMMANDS = frozenset(
    {
        "version",
        "login",
        "resume",
        "continue",
        "tools",
        "models",
        "providers",
        "observe",
        "skills",
        "mcp",
    }
)
_OPTIONS_WITH_VALUES = frozenset(
    {
        "--prompt",
        "--session",
        "--fork",
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
    preflight_error = _preflight_startup_error(argv)
    if preflight_error is not None:
        click.echo(f"Error: {preflight_error}", err=True)
        return 1
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


def _option_present(argv: list[str], option: str) -> bool:
    return option in argv or any(arg.startswith(f"{option}=") for arg in argv)


def _preflight_startup_error(argv: list[str]) -> str | None:
    if not argv or argv[0] in _SUBCOMMANDS or "--help" in argv or "-h" in argv:
        return None
    if _option_present(argv, "--image") and not _option_present(argv, "--prompt"):
        return "Interactive startup images require --prompt as well."
    if "--headless" not in argv:
        return None
    if _option_present(argv, "--prompt"):
        return None
    if sys.stdin.isatty():
        return "Headless mode requires --prompt or prompt text from stdin."
    try:
        prompt = input().strip()
    except EOFError:
        return "Headless mode requires --prompt or prompt text from stdin."
    if not prompt:
        return "Headless mode requires non-empty prompt text from stdin."
    argv.extend(["--prompt", prompt])
    return None


@tools_app.callback(invoke_without_command=True)
def _tools_callback(ctx: typer.Context) -> None:
    """Load the tools app only when a tools command is invoked."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


_LAZY_COMMAND_CONTEXT = {"allow_extra_args": True, "ignore_unknown_options": True}


@tools_app.command("init", context_settings=_LAZY_COMMAND_CONTEXT)
def _tools_init(ctx: typer.Context) -> None:
    """tools_init."""
    from yoke.cli.tools.app import tools_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "init", ctx.args))


@tools_app.command("activate", context_settings=_LAZY_COMMAND_CONTEXT)
def _tools_activate(ctx: typer.Context) -> None:
    """tools_activate."""
    from yoke.cli.tools.app import tools_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "activate", ctx.args))


@tools_app.command("deactivate", context_settings=_LAZY_COMMAND_CONTEXT)
def _tools_deactivate(ctx: typer.Context) -> None:
    """tools_deactivate."""
    from yoke.cli.tools.app import tools_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "deactivate", ctx.args))


@tools_app.command("list", context_settings=_LAZY_COMMAND_CONTEXT)
def _tools_list(ctx: typer.Context) -> None:
    """tools_list."""
    from yoke.cli.tools.app import tools_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "list", ctx.args))


@models_app.callback(invoke_without_command=True)
def _models_callback(ctx: typer.Context) -> None:
    """Load the models app only when a models command is invoked."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@models_app.command("list", context_settings=_LAZY_COMMAND_CONTEXT)
def _models_list(ctx: typer.Context) -> None:
    """List all provider-qualified models exposed by yoke providers."""
    from yoke.cli.models_app import models_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "list", ctx.args))


@models_app.command("set", context_settings=_LAZY_COMMAND_CONTEXT)
def _models_set(ctx: typer.Context) -> None:
    """Set the configured default model in yoke config."""
    from yoke.cli.models_app import models_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "set", ctx.args))


@providers_app.callback(invoke_without_command=True)
def _providers_callback(ctx: typer.Context) -> None:
    """Load the providers app only when a providers command is invoked."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@providers_app.command("login", context_settings=_LAZY_COMMAND_CONTEXT)
def _providers_login(ctx: typer.Context) -> None:
    """Interactively store credentials for a provider."""
    from yoke.cli.providers.app import providers_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "login", ctx.args))


@providers_app.command("list", context_settings=_LAZY_COMMAND_CONTEXT)
def _providers_list(ctx: typer.Context) -> None:
    """providers_list."""
    from yoke.cli.providers.app import providers_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "list", ctx.args))


@providers_app.command("doctor", context_settings=_LAZY_COMMAND_CONTEXT)
def _providers_doctor(ctx: typer.Context) -> None:
    """providers_doctor."""
    from yoke.cli.providers.app import providers_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "doctor", ctx.args))


@providers_app.command("init", context_settings=_LAZY_COMMAND_CONTEXT)
def _providers_init(ctx: typer.Context) -> None:
    """providers_init."""
    from yoke.cli.providers.app import providers_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "init", ctx.args))


@observe_app.callback(invoke_without_command=True)
def _observe_callback(ctx: typer.Context) -> None:
    """Load the observe app only when an observe command is invoked."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@observe_app.command("list", context_settings=_LAZY_COMMAND_CONTEXT)
def _observe_list(ctx: typer.Context) -> None:
    """List observed workflow runs."""
    from yoke.cli.observe_app import observe_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "list", ctx.args))


@observe_app.command("state", context_settings=_LAZY_COMMAND_CONTEXT)
def _observe_state(ctx: typer.Context) -> None:
    """Print the current projected state for a run."""
    from yoke.cli.observe_app import observe_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "state", ctx.args))


@observe_app.command("events", context_settings=_LAZY_COMMAND_CONTEXT)
def _observe_events(ctx: typer.Context) -> None:
    """Print observe events as JSON lines."""
    from yoke.cli.observe_app import observe_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "events", ctx.args))


@observe_app.command("watch", context_settings=_LAZY_COMMAND_CONTEXT)
def _observe_watch(ctx: typer.Context) -> None:
    """Watch new observe events as JSON lines."""
    from yoke.cli.observe_app import observe_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "watch", ctx.args))


@observe_app.command("serve", context_settings=_LAZY_COMMAND_CONTEXT)
def _observe_serve(ctx: typer.Context) -> None:
    """Serve observe runs over HTTP."""
    from yoke.cli.observe_app import observe_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "serve", ctx.args))


@skills_app.callback(invoke_without_command=True)
def _skills_callback(ctx: typer.Context) -> None:
    """Load the skills app only when a skills command is invoked."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@skills_app.command("list", context_settings=_LAZY_COMMAND_CONTEXT)
def _skills_list(ctx: typer.Context) -> None:
    """List discovered skills from built-in and default CLI skill directories."""
    from yoke.cli.skills_app import skills_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "list", ctx.args))


@skills_app.command("show", context_settings=_LAZY_COMMAND_CONTEXT)
def _skills_show(ctx: typer.Context) -> None:
    """Show the full contents of a discovered skill."""
    from yoke.cli.skills_app import skills_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "show", ctx.args))


@skills_app.command("init", context_settings=_LAZY_COMMAND_CONTEXT)
def _skills_init(ctx: typer.Context) -> None:
    """Create a new skill scaffold under .yoke/skills/<name>/SKILL.md."""
    from yoke.cli.skills_app import skills_app as loaded_app

    raise typer.Exit(_invoke_loaded_subcommand(loaded_app, "init", ctx.args))


def _invoke_loaded_subcommand(
    loaded_app: typer.Typer,
    command_name: str,
    args: list[str],
) -> int:
    try:
        result = loaded_app(
            args=[command_name, *args],
            prog_name=f"yoke {command_name}",
            standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    except typer.Exit as exc:
        return int(exc.exit_code)
    if isinstance(result, int):
        return result
    return 0


__all__ = [
    "CLIArgs",
    "PromptToolkitLiveRenderer",
    "app",
    "build_agent_from_args",
    "main",
    "run_continue_cli",
    "run_cli",
    "run_prompt_toolkit_cli",
    "run_resume_cli",
]

_EXPORTS = {
    "PromptToolkitLiveRenderer": (
        "yoke.cli.interactive",
        "PromptToolkitLiveRenderer",
    ),
    "build_agent_from_args": ("yoke.cli.config", "build_agent_from_args"),
    "run_continue_cli": ("yoke.cli.runtime", "run_continue_cli"),
    "run_cli": ("yoke.cli.runtime", "run_cli"),
    "run_prompt_toolkit_cli": ("yoke.cli.interactive", "run_prompt_toolkit_cli"),
    "run_resume_cli": ("yoke.cli.runtime", "run_resume_cli"),
}


def __getattr__(name: str) -> Any:
    """Lazily resolve compatibility exports from this module."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    module = __import__(module_name, fromlist=[attribute])
    value = getattr(module, attribute)
    globals()[name] = value
    return value
