"""Slash-command dispatch helpers for the interactive CLI."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.cli.config import CLIArgs
from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import resolve_image_path
from yoke.cli.interactive.model_commands import handle_switch_model
from yoke.cli.interactive.tree_selector import prompt_tree_label
from yoke.cli.interactive.tree_selector import (
    select_tree_entry_interactive,
)
from yoke.cli.interactive.tools_menu import handle_tools_menu
from yoke.cli.render.base import Console
from yoke.cli.render import format_compaction_note
from yoke.cli.render import print_session_scrollback
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime import force_compact_history
from yoke.cli.runtime import persist_session_state
from yoke.cli.session import fallback_session_title
from yoke.cli.runtime.selector_ui import select_list_item_interactive
from yoke.cli.runtime.tree import get_session_tree
from yoke.cli.runtime.tree import navigate_session_tree
from yoke.cli.runtime.tree import set_entry_label

COMPACTION_IN_PROGRESS_NOTICE = "Compacting conversation..."
SHORTCUTS_NOTICE = (
    "Keyboard shortcuts: Enter = steer/send, Tab = queue, Shift+Tab = "
    "cycle thinking effort, Esc Esc = stop current turn, Ctrl+J = "
    "newline, Ctrl+V = paste image or text, Ctrl+U = "
    "remove last pending image."
)


def handle_slash_command(  # noqa: C901
    command: str,
    *,
    agent: AgentRunner,
    active_session: ActiveSession,
    messages: list[Message],
    console: Console,
    pending_images: list[ImageAttachment] | None = None,
    on_context_usage: Callable[[dict[str, object]], None] | None = None,
    on_editor_text: Callable[[str], None] | None = None,
) -> tuple[bool, list[Message], ActiveSession]:
    """Handle slash commands and return updated state."""
    from yoke.cli.render import print_scrollback_notice
    from yoke.cli.runtime import create_active_session

    normalized = command.strip().lower()
    if normalized.startswith("/image "):
        if pending_images is None:
            print_scrollback_notice(
                console, "Image attachments are not available here."
            )
            return True, messages, active_session
        raw_path = command.strip()[len("/image ") :].strip()
        try:
            resolved = resolve_image_path(raw_path, root=active_session.root)
        except ValueError as exc:
            print_scrollback_notice(console, str(exc))
            return True, messages, active_session
        pending_images.append(ImageAttachment(path=resolved))
        print_scrollback_notice(console, f"Attached image: {resolved.name}")
        return True, messages, active_session
    if normalized == "/skill" or normalized.startswith("/skill "):
        _handle_skill_load(command, agent, active_session, messages, console)
        return True, messages, active_session
    if normalized == "/model" or normalized.startswith("/model "):
        handle_switch_model(
            command,
            agent=agent,
            active_session=active_session,
            messages=messages,
            console=console,
            args=CLIArgs(
                model=(
                    f"{active_session.record.provider_name}:"
                    f"{active_session.record.model_id}"
                    if active_session.record.provider_name
                    and active_session.record.model_id
                    else active_session.record.model_id
                ),
                reasoning_effort=active_session.record.reasoning_effort,
                root=str(active_session.root),
            ),
        )
        return True, messages, active_session
    if normalized == "/tools":
        handle_tools_menu(
            agent=agent,
            console=console,
            root=active_session.root,
        )
        return True, messages, active_session
    if normalized == "/title" or normalized.startswith("/title "):
        raw_title = command.strip()[len("/title") :].strip()
        if not raw_title:
            print_scrollback_notice(console, "Usage: /title <new-title>")
            return True, messages, active_session
        active_session.title = fallback_session_title(raw_title)
        active_session.record.title = active_session.title
        persist_session_state(active_session, agent, messages)
        print_scrollback_notice(
            console,
            f"Updated session title: {active_session.title}",
        )
        return True, messages, active_session
    if normalized == "/tree":
        if on_editor_text is None:
            print_scrollback_notice(
                console, "/tree is only available in the prompt-toolkit TUI."
            )
            return True, messages, active_session
        result = _handle_tree_command(
            active_session,
            agent,
            console,
            on_editor_text=on_editor_text,
        )
        if result is None:
            return True, messages, active_session
        return True, result[0], result[1]
    if normalized == "/compact":
        print_scrollback_notice(console, COMPACTION_IN_PROGRESS_NOTICE)
        compacted = force_compact_history(
            agent,
            messages,
            conversation_entries=active_branch_entries(
                active_session.record.conversation_entries,
                leaf_id=active_session.record.leaf_id,
            ),
        )
        if compacted is None:
            print_scrollback_notice(console, "Nothing to compact right now.")
            return True, messages, active_session
        (
            updated_messages,
            _preparation,
            _result,
            conversation_entries,
            compaction_payload,
            usage_payload,
        ) = compacted
        persist_session_state(
            active_session,
            agent,
            updated_messages,
            conversation_entries=conversation_entries,
        )
        print_scrollback_notice(
            console,
            format_compaction_note(compaction_payload),
        )
        if on_context_usage is not None:
            on_context_usage(usage_payload)
        return True, updated_messages, active_session
    if normalized in {"/shortcuts", "?"}:
        print_scrollback_notice(console, SHORTCUTS_NOTICE)
        return True, messages, active_session
    if normalized == "/new":
        new_session = create_active_session(
            CLIArgs(root=str(active_session.root)),
            root=active_session.root,
        )
        reset_agent = getattr(agent, "reset", None)
        if callable(reset_agent):
            reset_agent()
        persist_session_state(
            new_session,
            agent,
            [],
            conversation_entries=[],
        )
        print_scrollback_notice(
            console,
            f"Started new session {new_session.id}",
        )
        if on_context_usage is not None:
            on_context_usage({"usage_percent": 0})
        return True, [], new_session
    return False, messages, active_session


def _handle_tree_command(
    active_session: ActiveSession,
    agent: object,
    console: Console,
    *,
    on_editor_text: Callable[[str], None],
    initial_selected_id: str | None = None,
) -> tuple[list[Message], ActiveSession] | None:
    from yoke.cli.render import print_scrollback_notice

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
            return _handle_tree_command(
                active_session,
                agent,
                console,
                on_editor_text=on_editor_text,
                initial_selected_id=selection.entry_id,
            )
        set_entry_label(active_session, selection.entry_id, label)
        print_scrollback_notice(console, "Updated tree label.")
        return _handle_tree_command(
            active_session,
            agent,
            console,
            on_editor_text=on_editor_text,
            initial_selected_id=selection.entry_id,
        )
    if selection.entry_id is None:
        return None
    if selection.entry_id == active_session.record.leaf_id:
        print_scrollback_notice(console, "Already at this point.")
        return None
    choice = _ask_branch_summary_choice()
    if choice is None:
        return _handle_tree_command(
            active_session,
            agent,
            console,
            on_editor_text=on_editor_text,
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
    print_session_scrollback(console, result.messages)
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


def _handle_skill_load(
    command: str,
    agent: object,
    active_session: ActiveSession,
    messages: list[Message],
    console: Console,
) -> None:
    """Activate a discovered skill from a slash command."""
    from yoke.agent.loop import RuntimeAgent
    from yoke.cli.render import print_scrollback_notice

    skill_name = command.strip()[len("/skill") :].strip()
    if not skill_name:
        print_scrollback_notice(console, "Usage: /skill <name>")
        return
    if not isinstance(agent, RuntimeAgent) or agent.skill_registry is None:
        print_scrollback_notice(console, "No skills are available in this session.")
        return
    for skill in agent.active_skills:
        if skill.name != skill_name:
            continue
        skill.reload_on_next_use = True
        persist_session_state(active_session, agent, messages)
        print_scrollback_notice(
            console, f"Skill already active; reloading next use: {skill_name}"
        )
        return
    try:
        active_skill = agent.skill_registry.activate(skill_name)
    except KeyError:
        print_scrollback_notice(console, f"Unknown skill: {skill_name}")
        return
    agent.active_skills.append(active_skill)
    persist_session_state(active_session, agent, messages)
    print_scrollback_notice(console, f"Activated skill: {skill_name}")
