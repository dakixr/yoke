"""providers_app module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import typer
from rich.table import Table

from yoke.cli.config import BUILTIN_PROVIDER_NAMES
from yoke.cli.path_display import format_root_label
from yoke.cli.providers.registry import load_global_provider_plugins
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console

providers_app = typer.Typer(help="Manage dynamically loaded providers.")


def print_provider_inventory(stream: OutputStream) -> None:
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
    for plugin in load_global_provider_plugins(home=Path.home()):
        table.add_row(
            plugin.name,
            "global",
            format_root_label(plugin.source_path),
        )
    console.print(table)


def _set_user_env_var(name: str, value: str) -> None:
    """Persist a user-level environment variable on Windows via setx."""
    result = subprocess.run(  # noqa: S603
        ["setx", name, value],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        typer.echo(f"Failed to set {name}: {result.stderr.strip()}", err=True)
        raise typer.Exit(1)


LOGIN_PROVIDERS: dict[str, list[tuple[str, str]]] = {}


@providers_app.command("login")
def providers_login(
    name: str = typer.Argument(help="Provider name."),
) -> None:
    """Interactively store credentials for a provider."""
    name = name.strip().lower()
    fields = LOGIN_PROVIDERS.get(name)
    if fields is None:
        known = ", ".join(sorted(LOGIN_PROVIDERS)) or "(none)"
        typer.echo(f"Unknown provider '{name}'. Known providers: {known}", err=True)
        raise typer.Exit(1)

    console = build_console(cast(OutputStream, sys.stdout))
    console.print(f"\n[bold]Login to {name}[/bold]")

    values: dict[str, str] = {}
    for env_key, label in fields:
        is_secret = "password" in label.lower()
        existing = os.getenv(env_key, "")
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

    for env_key, value in values.items():
        _set_user_env_var(env_key, value)
        os.environ[env_key] = value

    console.print(
        "\n[green]Credentials saved.[/green] "
        "Restart your terminal for changes to take full effect.\n"
    )


@providers_app.command("list")
def providers_list() -> None:
    """providers_list."""
    print_provider_inventory(cast(OutputStream, sys.stdout))


@providers_app.command("doctor")
def providers_doctor() -> None:
    """providers_doctor."""
    console = build_console(cast(OutputStream, sys.stdout))
    try:
        load_global_provider_plugins(home=Path.home())
    except ValueError as exc:
        typer.echo(f"Provider loading failed: {exc}")
        raise typer.Exit(1) from exc
    console.print("Provider loading OK.")
    print_provider_inventory(cast(OutputStream, sys.stdout))


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
