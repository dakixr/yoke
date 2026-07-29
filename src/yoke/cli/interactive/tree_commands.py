"""Conversation-tree slash-command helpers."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

from yoke.agent.models import Message
from yoke.cli.interactive.tree_selector import prompt_tree_label
from yoke.cli.interactive.tree_selector import select_tree_entry_interactive
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_session_scrollback
from yoke.cli.render.base import Console
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime.selector.ui import select_list_item_interactive
from yoke.cli.runtime.tree import get_session_tree
from yoke.cli.runtime.tree import navigate_session_tree
from yoke.cli.runtime.tree import set_entry_label


def handle_tree_command(
    active_session: ActiveSession,
    agent: object,
    console: Console,
    *,
    on_editor_text: Callable[[str], None],
    on_replay_messages: Callable[[list[Message]], None] | None = None,
    initial_selected_id: str | None = None,
) -> tuple[list[Message], ActiveSession] | None:
    """Navigate to a selected conversation entry."""
    roots = get_session_tree(active_session)
    if not roots:
        print_scrollback_notice(console, "No session entries yet.")
        return None
    selection = select_tree_entry_interactive(
        roots,
        current_leaf_id=active_session.record.leaf_id,
        initial_selected_id=initial_selected_id,
    )
    if selection is None:
        print_scrollback_notice(console, "Tree navigation cancelled.")
        return None
    if selection.action == "label" and selection.entry_id is not None:
        label = prompt_tree_label()
        if label is None:
            return handle_tree_command(
                active_session,
                agent,
                console,
                on_editor_text=on_editor_text,
                on_replay_messages=on_replay_messages,
                initial_selected_id=selection.entry_id,
            )
        set_entry_label(active_session, selection.entry_id, label)
        print_scrollback_notice(console, "Updated tree label.")
        return handle_tree_command(
            active_session,
            agent,
            console,
            on_editor_text=on_editor_text,
            on_replay_messages=on_replay_messages,
            initial_selected_id=selection.entry_id,
        )
    if selection.entry_id is None:
        return None
    if selection.entry_id == active_session.record.leaf_id:
        print_scrollback_notice(console, "Already at this point.")
        return None
    choice = _ask_branch_summary_choice()
    if choice is None:
        return handle_tree_command(
            active_session,
            agent,
            console,
            on_editor_text=on_editor_text,
            on_replay_messages=on_replay_messages,
            initial_selected_id=selection.entry_id,
        )
    summarize, custom_instructions = choice
    print_scrollback_notice(console, "Navigating session tree...")
    result = navigate_session_tree(
        active_session,
        agent,
        selection.entry_id,
        summarize=summarize,
        custom_instructions=custom_instructions,
    )
    if result.editor_text is not None:
        on_editor_text(result.editor_text)
    if on_replay_messages is None:
        print_session_scrollback(console, result.messages)
    else:
        on_replay_messages(result.messages)
    if result.summary_error is not None:
        print_scrollback_notice(
            console,
            f"Branch summary failed; navigated without it: {result.summary_error}",
        )
    suffix = " with branch summary" if result.summary_created else ""
    print_scrollback_notice(console, f"Navigated to selected point{suffix}.")
    return result.messages, result.active_session


def _ask_branch_summary_choice() -> tuple[bool, str | None] | None:
    choices = [
        "No summary",
        "Summarize",
        "Summarize with custom instructions",
    ]
    choice = select_list_item_interactive(
        choices,
        title="Branch Summary",
        subtitle="Choose whether to summarize the branch you are leaving.",
        render_item=lambda item, _index, _selected, _columns: item,
        footer="enter select · esc returns to tree",
    )
    if choice is None:
        return None
    if choice == "No summary":
        return False, None
    if choice == "Summarize":
        return True, None
    custom = _prompt_custom_summary_instructions()
    if custom is None:
        return None
    return True, custom


def _prompt_custom_summary_instructions() -> str | None:
    from prompt_toolkit import prompt

    with suppress(EOFError, KeyboardInterrupt):
        return prompt("Custom summary guidance: ")
    return None
