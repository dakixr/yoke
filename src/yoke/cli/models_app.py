"""Top-level CLI app for model catalog inspection and default-model config."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated
from typing import cast

import typer
from rich.table import Table
from rich.text import Text

from yoke.ai.providers.model_selection import compatible_reasoning_effort_for_model
from yoke.ai.providers.resolution import list_provider_models
from yoke.cli.config import CLIArgs
from yoke.cli.config import load_effective_yoke_config
from yoke.cli.path_display import format_root_label
from yoke.cli.providers.catalog import list_all_provider_model_choices
from yoke.cli.providers.catalog import parse_provider_model_identifier
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.runtime.selector.ui import can_use_keyboard_selector
from yoke.cli.runtime.selector.ui import SelectorTableColumns
from yoke.cli.runtime.selector.ui import select_table_item_interactive
from yoke.cli.tools.policy import PiConfig

DEFAULT_ROOT = Path.cwd().absolute()

models_app = typer.Typer(
    help="Inspect available models and configure the default model."
)


def _config_path(*, root: Path, global_scope: bool, repo_scope: bool) -> Path:
    if global_scope and repo_scope:
        typer.echo("Use either --global or --repo, not both.", err=True)
        raise typer.Exit(2)
    if repo_scope:
        return root / ".yoke" / "config.json"
    return Path.home() / ".yoke" / "config.json"


def _load_config(path: Path) -> PiConfig:
    if not path.is_file():
        return PiConfig()
    try:
        return PiConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            "Could not update default model because "
            f"`{path}` is invalid. Fix or remove that file first. {exc}"
        ) from exc


def _write_default_model_config(
    path: Path,
    *,
    default_model: str,
    default_reasoning_effort: str | None,
) -> None:
    config = _load_config(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            PiConfig(
                capabilities=dict(config.capabilities),
                tools=dict(config.tools),
                default_model=default_model,
                default_reasoning_effort=default_reasoning_effort,
            ).model_dump(mode="json", exclude_none=True),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def set_default_model(
    default_model: str,
    *,
    root: Path,
    reasoning_effort: str | None = None,
    global_scope: bool = False,
    repo_scope: bool = False,
) -> Path:
    """Persist the configured default model and return the config path."""
    provider_name, model_name = parse_provider_model_identifier(default_model)
    normalized = f"{provider_name}:{model_name}"
    models = list_provider_models(
        provider_name,
        model=model_name,
        reasoning_effort=None,
        home=Path.home(),
    )
    selected = next((model for model in models or () if model.id == model_name), None)
    resolved_effort = (
        compatible_reasoning_effort_for_model(selected, reasoning_effort)
        if selected is not None
        else reasoning_effort
    )
    path = _config_path(
        root=root,
        global_scope=global_scope,
        repo_scope=repo_scope,
    )
    _write_default_model_config(
        path,
        default_model=normalized,
        default_reasoning_effort=resolved_effort,
    )
    return path


def _prompt_for_default_model(
    *,
    root: Path,
    reasoning_effort: str | None = None,
) -> str:
    choices = list_all_provider_model_choices(
        args=CLIArgs(root=str(root), reasoning_effort=reasoning_effort)
    )
    if not choices:
        raise ValueError("No models advertised by providers.")
    qualified_ids = [choice.qualified_id for choice in choices]
    if can_use_keyboard_selector(sys.stdout):
        selected = _select_model_interactive(qualified_ids, root=root)
        if selected is None:
            raise ValueError("Model selection cancelled.")
        return selected
    return _select_model_by_number(qualified_ids)


def _select_model_by_number(qualified_ids: list[str]) -> str:
    console = build_console(cast(OutputStream, sys.stdout))
    console.print("Select a default model:")
    for index, qualified_id in enumerate(qualified_ids, start=1):
        console.print(f"{index}. {qualified_id}")
    raw = input("Model number: ").strip()
    try:
        selected = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid model number: {raw!r}") from exc
    if selected < 1 or selected > len(qualified_ids):
        raise ValueError(f"Model number must be between 1 and {len(qualified_ids)}.")
    return qualified_ids[selected - 1]


def _select_model_interactive(
    qualified_ids: list[str],
    *,
    root: Path,
) -> str | None:
    index_width = max(3, len(str(len(qualified_ids))) + 2)
    model_width = max(
        len("Model"),
        max((len(model_id) for model_id in qualified_ids), default=0),
    )
    return select_table_item_interactive(
        qualified_ids,
        title="Select a default model for new sessions:",
        subtitle=(
            "Default scope: global (`~\\.yoke\\config.json`).\n"
            "For this repo, use `yoke models set --repo`."
        ),
        columns=SelectorTableColumns(
            headers=("#", "Model"),
            widths=(index_width, model_width),
        ),
        render_row=_render_model_selector_row,
        footer=(
            "Use Up/Down or j/k, PgUp/PgDn, Home/End, Enter to select, q to cancel."
        ),
    )


def _render_model_selector_row(
    qualified_id: str,
    index: int,
    is_selected: bool,
    columns: SelectorTableColumns,
) -> str:
    marker = ">" if is_selected else " "
    return "  ".join(
        (
            f"{marker} {index + 1:>{max(1, columns.widths[0] - 2)}}",
            qualified_id.ljust(columns.widths[1]),
        )
    )


def print_model_inventory(
    stream: OutputStream,
    *,
    root: Path,
    reasoning_effort: str | None = None,
) -> None:
    """Print the provider-qualified model catalog known to the CLI."""
    console = build_console(stream)
    effective_config = load_effective_yoke_config(root=root)
    table = Table(
        title="Model Inventory",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Model", style="bold")
    table.add_column("Default")
    table.add_column("Context")
    table.add_column("Thinking")
    choices = list_all_provider_model_choices(
        args=CLIArgs(root=str(root), reasoning_effort=reasoning_effort)
    )
    for choice in choices:
        is_default = effective_config.default_model == choice.qualified_id
        table.add_row(
            choice.qualified_id,
            Text("default" if is_default else "", style="green"),
            str(choice.model.context_window_tokens),
            ", ".join(choice.model.thinking_levels),
        )
    console.print(table)
    if effective_config.default_model is not None:
        console.print(f"Configured default model: {effective_config.default_model}")
    else:
        console.print("Configured default model: none")
    if effective_config.default_reasoning_effort is not None:
        console.print(
            "Configured default reasoning effort: "
            f"{effective_config.default_reasoning_effort}"
        )
    else:
        console.print("Configured default reasoning effort: none")


@models_app.command("list")
def models_list(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root used for repo-local config resolution.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = DEFAULT_ROOT,
    reasoning_effort: Annotated[
        str | None,
        typer.Option(
            "--reasoning-effort",
            help="Optional thinking level passed to provider model discovery.",
        ),
    ] = None,
) -> None:
    """List all provider-qualified models exposed by yoke providers."""
    print_model_inventory(
        cast(OutputStream, sys.stdout),
        root=root,
        reasoning_effort=reasoning_effort,
    )


@models_app.command("set")
def models_set(
    model: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Default model as provider-name:model-name. "
                "If omitted, yoke prompts you to choose."
            ),
        ),
    ] = None,
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root whose .yoke/config.json should be updated.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = DEFAULT_ROOT,
    reasoning_effort: Annotated[
        str | None,
        typer.Option(
            "--reasoning-effort",
            help=(
                "Persist the default reasoning effort alongside the default "
                "model: none, low, medium, high, xhigh, or max."
            ),
        ),
    ] = None,
    global_scope: Annotated[
        bool,
        typer.Option(
            "--global",
            help="Update ~/.yoke/config.json (default behavior).",
        ),
    ] = False,
    repo_scope: Annotated[
        bool,
        typer.Option(
            "--repo",
            help=("Update the repo .yoke/config.json instead of the global config."),
        ),
    ] = False,
) -> None:
    """Set the configured default model in yoke config."""
    try:
        target_model = model or _prompt_for_default_model(root=root)
        path = set_default_model(
            target_model,
            root=root,
            reasoning_effort=reasoning_effort,
            global_scope=global_scope,
            repo_scope=repo_scope,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        exit_code = 2 if "provider-name:model-name" in str(exc) else 1
        raise typer.Exit(exit_code) from exc
    config = _load_config(path)
    message = f"Set default_model={config.default_model}"
    if config.default_reasoning_effort is not None:
        message += f" default_reasoning_effort={config.default_reasoning_effort}"
    typer.echo(f"{message} in {format_root_label(path)}")


__all__ = [
    "DEFAULT_ROOT",
    "models_app",
    "models_list",
    "models_set",
    "print_model_inventory",
    "set_default_model",
]
