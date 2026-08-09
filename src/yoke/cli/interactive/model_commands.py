"""Interactive slash-command helpers for provider model switching."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import Message
from yoke.agent.state import capture_agent_state
from yoke.cli.config import CLIArgs
from yoke.cli.providers.catalog import ProviderModelChoice
from yoke.cli.providers.catalog import list_all_provider_model_choices
from yoke.cli.providers.state import switch_agent_provider_model
from yoke.cli.render.base import Console
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime.session import save_active_session_metadata
from yoke.cli.runtime.selector.format import fit_selector_cell
from yoke.cli.runtime.selector.ui import SelectorTableColumns
from yoke.cli.runtime.selector.ui import select_table_item_interactive


@dataclass(slots=True, frozen=True)
class _ModelSelectorRow:
    choice: ProviderModelChoice
    is_current: bool


def handle_switch_model(
    command: str,
    *,
    agent: object,
    active_session: ActiveSession,
    messages: list[Message],
    console: Console,
    args: CLIArgs,
) -> list[Message]:
    """Open the interactive model switcher from a slash command."""
    from yoke.cli.render import print_scrollback_notice

    raw_args = command.strip()[len("/model") :].strip()
    if raw_args:
        print_scrollback_notice(console, "Usage: /model")
        return messages

    selected = _select_provider_model(
        console,
        args=args,
        active_session=active_session,
    )
    if selected is None:
        return messages
    return _switch_model(
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
    messages: list[Message],
    console: Console,
    args: CLIArgs,
) -> list[Message]:
    from yoke.cli.render import print_scrollback_notice

    context_before_switch = _context_marker(agent)
    try:
        provider_state = switch_agent_provider_model(
            agent,
            args=args,
            qualified_model_id=qualified_model_id,
            reasoning_effort=reasoning_effort,
        )
    except ValueError as exc:
        print_scrollback_notice(console, str(exc))
        return messages
    if _context_marker(agent) == context_before_switch:
        try:
            save_active_session_metadata(active_session, provider_state)
        except OSError as exc:
            print_scrollback_notice(
                console,
                f"Model switched, but session metadata was not saved: {exc}",
            )
        updated_messages = messages
    else:
        state = capture_agent_state(agent)
        runtime_messages = getattr(agent, "messages", None)
        updated_messages = (
            [message.model_copy(deep=True) for message in runtime_messages]
            if isinstance(runtime_messages, list)
            else state.messages
        )
        persist_session_state(
            active_session,
            agent,
            updated_messages,
            conversation_entries=state.conversation_entries,
        )
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
    return updated_messages


def _context_marker(agent: object) -> tuple[int, str | None, str | None] | None:
    context = getattr(agent, "_context", None)
    if context is None:
        return None
    log = getattr(context, "conversation_log", None)
    from yoke.agent.conversation import project_conversation

    snapshot = project_conversation(
        getattr(log, "entries", ()),
        leaf_id=getattr(log, "leaf_id", None),
    ).checkpoint
    return (
        id(context),
        getattr(log, "leaf_id", None),
        getattr(snapshot, "id", None),
    )


def _select_provider_model(
    console: Console,
    *,
    args: CLIArgs,
    active_session: ActiveSession,
) -> ProviderModelChoice | None:
    from yoke.cli.render import print_scrollback_notice

    choices = list_all_provider_model_choices(args=args)
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
        headers=("", "Provider", "Model", "Context", "Thinking"),
        widths=(
            2,
            max(
                len("Provider"),
                max(len(row.choice.provider_name) for row in rows),
            ),
            max(len("Model"), max(len(row.choice.model.id) for row in rows)),
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
            str(row.choice.model.context_window_tokens).rjust(columns.widths[3]),
            fit_selector_cell(
                ", ".join(row.choice.model.thinking_levels),
                columns.widths[4],
            ),
        )
    )
