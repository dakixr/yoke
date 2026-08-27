"""Typer command handlers for the yoke CLI."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from yoke.cli.main_core import CWD
from yoke.cli.main_core import _run_lazy_typer_app
from yoke.cli.main_core import build_cli_args


def register_commands(app: typer.Typer) -> None:
    """Attach all CLI command handlers to *app*."""

    @app.command()
    def version() -> None:
        """Print the yoke version and exit."""
        import click

        from yoke._version import __version__

        click.echo(__version__)

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
                    "Run one non-interactive prompt and exit. "
                    "Requires --prompt or piped stdin."
                ),
            ),
        ] = False,
        session: Annotated[
            str | None,
            typer.Option(
                "--session",
                help=(
                    "Persist conversation under "
                    "[bold].yoke/sessions/<name>.json[/bold]."
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
                help=(
                    "Model to send to the provider. "
                    "Use `provider-name:model-name` to select a specific "
                    "provider, or just `model-name` to let yoke pick a "
                    "provider from available credentials."
                ),
            ),
        ] = None,
        reasoning_effort: Annotated[
            str | None,
            typer.Option(
                "--reasoning-effort",
                help=(
                    "Reasoning effort for supported chat-completions models: "
                    "none, low, medium, high, xhigh, or max."
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
            import click

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
            typer.Argument(help="Provider name to login to (e.g. codex)."),
        ],
    ) -> None:
        """Interactively store credentials for a provider."""
        from yoke.cli.providers.app import providers_login

        providers_login(name)

    @app.command()
    def serve(
        host: Annotated[
            str,
            typer.Option("--host", help="HTTP bind host. Defaults to loopback only."),
        ] = "127.0.0.1",
        port: Annotated[
            int,
            typer.Option(
                "--port",
                min=0,
                max=65535,
                help="HTTP port. Use 0 for an ephemeral port.",
            ),
        ] = 8765,
        token: Annotated[
            str | None,
            typer.Option(
                "--token",
                envvar="YOKE_HTTP_TOKEN",
                help="Bearer token. Generated when omitted.",
            ),
        ] = None,
        allow_remote: Annotated[
            bool,
            typer.Option(
                "--allow-remote",
                help="Permit binding to a non-loopback host.",
            ),
        ] = False,
        open_browser: Annotated[
            bool,
            typer.Option(
                "--open",
                help="Open the web UI with the bearer token already configured.",
            ),
        ] = False,
        verbose: Annotated[
            bool,
            typer.Option(
                "--verbose",
                help="Show Uvicorn lifecycle and access logs.",
            ),
        ] = False,
    ) -> None:
        """Run the process-wide Yoke HTTP API."""
        import click

        from yoke.http.server import run_server

        try:
            result = run_server(
                host=host,
                port=port,
                auth_token=token,
                allow_remote=allow_remote,
                open_browser=open_browser,
                verbose=verbose,
            )
        except (OSError, ValueError) as exc:
            click.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        raise typer.Exit(result)

    _DELEGATE_CONTEXT_SETTINGS = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
    }

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
                help=(
                    "Show sessions from all workspace roots when choosing a session."
                ),
            ),
        ] = False,
        model: Annotated[
            str | None,
            typer.Option(
                "--model",
                help=(
                    "Model to send to the provider. "
                    "Use `provider-name:model-name` "
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
                    "none, low, medium, high, xhigh, or max."
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

    @app.command(
        context_settings=_DELEGATE_CONTEXT_SETTINGS,
        help="Manage dynamically loaded tools.",
    )
    def tools(ctx: typer.Context) -> None:
        """Delegate to the tools sub-application."""
        from yoke.cli.tools.app import tools_app

        raise typer.Exit(_run_lazy_typer_app(tools_app, list(ctx.args), "yoke tools"))

    @app.command(
        context_settings=_DELEGATE_CONTEXT_SETTINGS,
        help="Inspect available models and configure the default model.",
    )
    def models(ctx: typer.Context) -> None:
        """Delegate to the models sub-application."""
        from yoke.cli.models_app import models_app

        raise typer.Exit(_run_lazy_typer_app(models_app, list(ctx.args), "yoke models"))

    @app.command(
        context_settings=_DELEGATE_CONTEXT_SETTINGS,
        help="Manage dynamically loaded providers.",
    )
    def providers(ctx: typer.Context) -> None:
        """Delegate to the providers sub-application."""
        from yoke.cli.providers.app import providers_app

        raise typer.Exit(
            _run_lazy_typer_app(providers_app, list(ctx.args), "yoke providers")
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
        import click

        from yoke.cli.mcp_app import format_mcp_status

        click.echo(format_mcp_status(root=root, home=Path.home(), server=server))

    @app.command(
        context_settings=_DELEGATE_CONTEXT_SETTINGS,
        help=(
            "Manage skills. The CLI discovers built-in skills from the yoke "
            "codebase plus ~/.yoke/skills and <repo>/.yoke/skills by default."
        ),
    )
    def skills(ctx: typer.Context) -> None:
        """Delegate to the skills sub-application."""
        from yoke.cli.skills_app import skills_app

        raise typer.Exit(_run_lazy_typer_app(skills_app, list(ctx.args), "yoke skills"))
