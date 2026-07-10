"""providers_app module."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

import typer
from rich.table import Table

from yoke.ai.providers.credentials import provider_credentials_path
from yoke.ai.providers.credentials import provider_environment
from yoke.ai.providers.credentials import save_provider_credential
from yoke.ai.providers.plugins import LoadedProviderPlugin
from yoke.ai.providers.plugins import discover_global_provider_plugins
from yoke.ai.providers.plugins import load_global_provider_plugins
from yoke.cli.config.providers import BUILTIN_PROVIDER_NAMES
from yoke.cli.path_display import format_root_label
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console

providers_app = typer.Typer(help="Manage dynamically loaded providers.")


def print_provider_inventory(
    stream: OutputStream,
    *,
    plugins: list[LoadedProviderPlugin] | None = None,
) -> None:
    """print_provider_inventory."""
    console = build_console(stream)
    table = Table(
        title="Provider Inventory",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Name", style="bold")
    table.add_column("Kind")
    table.add_column("Location")
    for name in BUILTIN_PROVIDER_NAMES:
        table.add_row(name, "builtin", "builtin")
    loaded_plugins = (
        load_global_provider_plugins(home=Path.home()) if plugins is None else plugins
    )
    for plugin in loaded_plugins:
        table.add_row(
            plugin.name,
            "global",
            format_root_label(plugin.source_path),
        )
    console.print(table)


LOGIN_PROVIDERS: dict[str, list[tuple[str, str]]] = {
    "opencode-go": [("OPENCODE_API_KEY", "API key")],
    "zai": [("ZAI_API_KEY", "API key")],
}


@providers_app.command("login")
def providers_login(
    name: str = typer.Argument(help="Provider name."),
) -> None:
    """Interactively store credentials for a provider."""
    name = name.strip().lower()
    if name == "codex":
        _login_codex()
        return
    fields = LOGIN_PROVIDERS.get(name)
    if fields is None:
        known = ", ".join(["codex", *sorted(LOGIN_PROVIDERS)])
        typer.echo(f"Unknown provider '{name}'. Known providers: {known}", err=True)
        raise typer.Exit(1)

    console = build_console(cast(OutputStream, sys.stdout))
    console.print(f"\n[bold]Login to {name}[/bold]")

    home = Path.home()
    try:
        current_env = provider_environment(home=home, env=os.environ)
    except ValueError as exc:
        typer.echo(f"Could not read existing provider credentials: {exc}", err=True)
        raise typer.Exit(1) from exc
    values: dict[str, str] = {}
    for env_key, label in fields:
        is_secret = any(
            marker in env_key.lower() for marker in ("key", "password", "token")
        )
        existing = current_env.get(env_key, "")
        if existing and not is_secret:
            console.print(f"Current {label.lower()} is already configured.")
        prompt_text = f"{label}"
        if existing:
            prompt_text += " (press Enter to keep current)"
        value: str = typer.prompt(
            prompt_text,
            hide_input=is_secret,
            default="",
            show_default=False,
        )
        if not value and existing:
            value = existing
        if not value:
            typer.echo(f"{label} is required.", err=True)
            raise typer.Exit(2)
        values[env_key] = value

    credential_path = provider_credentials_path(home)
    try:
        for env_key, value in values.items():
            credential_path = save_provider_credential(
                home=home,
                name=env_key,
                value=value,
            )
            os.environ[env_key] = value
    except (OSError, ValueError) as exc:
        typer.echo(f"Could not save {name} credentials: {exc}", err=True)
        raise typer.Exit(1) from exc

    console.print(
        f"\n[green]Credentials saved to {format_root_label(credential_path)}.[/green]\n"
    )


def _login_codex() -> None:
    """Run Codex OAuth and persist the resulting fallback credentials."""
    from yoke.ai.providers.codex.subscription import AuthStorage
    from yoke.ai.providers.codex.subscription import OAUTH_PROVIDER_ID
    from yoke.ai.providers.codex.subscription import login_openai_codex

    auth_path = Path.home() / ".codex" / "auth.json"
    try:
        credentials = login_openai_codex("yoke")
        AuthStorage(auth_path).set_oauth(OAUTH_PROVIDER_ID, credentials)
    except Exception as exc:
        typer.echo(f"Codex login failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Codex credentials saved to {format_root_label(auth_path)}")


@providers_app.command("list")
def providers_list() -> None:
    """providers_list."""
    print_provider_inventory(cast(OutputStream, sys.stdout))


@providers_app.command("doctor")
def providers_doctor() -> None:
    """providers_doctor."""
    console = build_console(cast(OutputStream, sys.stdout))
    discovery = discover_global_provider_plugins(home=Path.home())
    if discovery.failures:
        console.print(
            f"[red]Provider loading completed with "
            f"{len(discovery.failures)} failure(s).[/red]"
        )
        for failure in discovery.failures:
            console.print(f"[red]- {format_root_label(failure.source_path)}[/red]")
            console.print(f"[red]  {failure.error}[/red]")
        print_provider_inventory(
            cast(OutputStream, sys.stdout),
            plugins=list(discovery.plugins),
        )
        raise typer.Exit(1)
    console.print("[green]Provider loading OK.[/green]")
    print_provider_inventory(
        cast(OutputStream, sys.stdout),
        plugins=list(discovery.plugins),
    )


@providers_app.command("init")
def providers_init(
    name: str = typer.Argument(help="Provider module name to create."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite the provider file if it exists."
    ),
) -> None:
    """providers_init."""
    safe_name = name.strip().lower().replace("-", "_")
    if not safe_name or not safe_name.isidentifier():
        typer.echo("Provider name must be a valid Python identifier.", err=True)
        raise typer.Exit(2)
    target = Path.home() / ".yoke" / "providers" / f"{safe_name}.py"
    if target.exists() and not force:
        typer.echo(f"Refusing to overwrite existing file: {format_root_label(target)}")
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_provider_template(safe_name), encoding="utf-8")
    typer.echo(f"Created provider scaffold at {format_root_label(target)}")


def _provider_template(name: str) -> str:
    return f'''from __future__ import annotations

import os

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    build_model_catalog,
)


PROVIDER_NAME = "{name}"


def list_provider_models(context):
    return list(
        build_model_catalog(
            ProviderModelInfo(
                id="model-name",
                display_name="Model Name",
                context_window_tokens=128000,
                thinking_levels=(
                    "none",
                                        "low",
                    "medium",
                    "high",
                    "xhigh",
                ),
                supports_image_inputs=True,
            )
        )
    )


def register_provider(context):
    api_key = os.getenv("{name.upper()}_API_KEY", "")
    if not api_key:
        raise ValueError(
            "{name} provider requires the {name.upper()}_API_KEY env var."
        )
    return OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key=api_key,
            model=context.model or "model-name",
            base_url="https://example.com/v1",
            reasoning_effort=context.reasoning_effort,
            provider_name=PROVIDER_NAME,
            model_catalog=tuple(list_provider_models(context)),
        )
    )
'''
