"""Interactive slash-command helpers for provider model switching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yoke.cli.config import CLIArgs
from yoke.cli.providers.catalog import ProviderModelChoice
from yoke.cli.providers.catalog import list_all_provider_model_choices
from yoke.cli.providers.state import switch_agent_provider_model
from yoke.cli.render.base import Console
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime.selector_format import fit_selector_cell
from yoke.cli.runtime.selector_ui import SelectorTableColumns
from yoke.cli.runtime.selector_ui import select_table_item_interactive


@dataclass(slots=True, frozen=True)
class _ModelSelectorRow:
    choice: ProviderModelChoice
    is_current: bool


def handle_switch_model(
    command: str,
    *,
    agent: object,
    active_session: ActiveSession,
    messages: list,
    console: Console,
    args: CLIArgs,
) -> None:
    """Open the interactive model switcher from a slash command."""
    from yoke.cli.render import print_scrollback_notice

    raw_args = command.strip()[len("/model") :].strip()
    if raw_args:
        print_scrollback_notice(console, "Usage: /model")
        return

    selected = _select_provider_model(
        console,
        args=args,
        active_session=active_session,
    )
    if selected is None:
        return
    _switch_model(
        selected.qualified_id,
        None,
        agent=agent,
        active_session=active_session,
        messages=messages,
        console=console,
        args=args,
    )


def _switch_model(
    qualified_model_id: str,
    reasoning_effort: str | None,
    *,
    agent: object,
    active_session: ActiveSession,
    messages: list,
    console: Console,
    args: CLIArgs,
) -> None:
    from yoke.cli.render import print_scrollback_notice

    try:
        provider_state = switch_agent_provider_model(
            agent,
            args=args,
            qualified_model_id=qualified_model_id,
            reasoning_effort=reasoning_effort,
        )
    except ValueError as exc:
        message = str(exc)
        if message.startswith("compact before switching."):
            message = f"{message} Run /compact and try again."
        print_scrollback_notice(console, message)
        return
    persist_session_state(active_session, agent, messages)
    context_suffix = (
        f", ctx={provider_state.context_window_tokens}"
        if provider_state.context_window_tokens is not None
        else ""
    )
    effort_suffix = (
        f", thinking={provider_state.reasoning_effort}"
        if provider_state.reasoning_effort is not None
        else ""
    )
    print_scrollback_notice(
        console,
        "model switched to "
        f"{provider_state.provider_name}:{provider_state.model_id}"
        f"{context_suffix}{effort_suffix}",
    )


def _select_provider_model(
    console: Console,
    *,
    args: CLIArgs,
    active_session: ActiveSession,
) -> ProviderModelChoice | None:
    from yoke.cli.render import print_scrollback_notice

    choices = list_all_provider_model_choices(args=args, home=Path.home())
    if not choices:
        print_scrollback_notice(console, "No models advertised by providers.")
        return None

    rows = [
        _ModelSelectorRow(
            choice=choice,
            is_current=(
                choice.provider_name == active_session.record.provider_name
                and choice.model.id == active_session.record.model_id
            ),
        )
        for choice in choices
    ]
    selected = select_table_item_interactive(
        rows,
        title="Switch model:",
        subtitle="Current model is marked with `*`.",
        columns=_model_selector_columns(rows),
        render_row=_render_model_selector_row,
        footer=(
            "Use Up/Down or j/k, PgUp/PgDn, Home/End, Enter to switch, q to cancel."
        ),
    )
    if selected is None:
        print_scrollback_notice(console, "Model switch cancelled.")
        return None
    return selected.choice


def _model_selector_columns(
    rows: list[_ModelSelectorRow],
) -> SelectorTableColumns:
    return SelectorTableColumns(
        headers=("", "Provider", "Model", "Images", "Context", "Thinking"),
        widths=(
            2,
            max(
                len("Provider"),
                max(len(row.choice.provider_name) for row in rows),
            ),
            max(len("Model"), max(len(row.choice.model.id) for row in rows)),
            len("Images"),
            max(
                len("Context"),
                max(len(str(row.choice.model.context_window_tokens)) for row in rows),
            ),
            min(
                42,
                max(
                    len("Thinking"),
                    max(
                        len(", ".join(row.choice.model.thinking_levels)) for row in rows
                    ),
                ),
            ),
        ),
    )


def _render_model_selector_row(
    row: _ModelSelectorRow,
    _index: int,
    is_cursor: bool,
    columns: SelectorTableColumns,
) -> str:
    marker = ">" if is_cursor else " "
    current = "*" if row.is_current else " "
    return "  ".join(
        (
            f"{marker}{current}".ljust(columns.widths[0]),
            row.choice.provider_name.ljust(columns.widths[1]),
            row.choice.model.id.ljust(columns.widths[2]),
            _format_image_support(row.choice.model.supports_image_inputs).ljust(
                columns.widths[3]
            ),
            str(row.choice.model.context_window_tokens).rjust(columns.widths[4]),
            fit_selector_cell(
                ", ".join(row.choice.model.thinking_levels),
                columns.widths[5],
            ),
        )
    )


def _format_image_support(supports_image_inputs: bool | None) -> str:
    if supports_image_inputs is True:
        return "yes"
    if supports_image_inputs is False:
        return "no"
    return "unknown"
