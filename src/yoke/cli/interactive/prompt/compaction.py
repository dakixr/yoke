"""Prompt-toolkit compaction worker control."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from threading import Thread

from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.queue.persistence import persist_prompt_queue
from yoke.cli.render import format_compaction_note
from yoke.cli.render import print_scrollback_notice
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime import force_compact_history
from yoke.cli.runtime import persist_session_state


def start_prompt_compaction(
    *,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session_ref: dict[str, ActiveSession],
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
    estimate_toolbar_context_usage: Callable[[str], str | None],
    update_status: Callable[[str], None],
    invalidate_prompt: Callable[[], None],
    start_turn: Callable[[str, Message | None], Thread],
) -> Thread:
    """Run forced compaction in the active-worker slot."""
    run_in_scrollback(
        lambda: print_scrollback_notice(
            scrollback_console,
            "Compacting conversation...",
        )
    )
    with state_lock:
        message_snapshot = list(state.messages)
        current_session = active_session_ref["active_session"]
        conversation_entries_snapshot = active_branch_entries(
            current_session.record.conversation_entries,
            leaf_id=current_session.record.leaf_id,
        )

    def run_compaction() -> None:
        compacted = force_compact_history(
            agent,
            message_snapshot,
            conversation_entries=conversation_entries_snapshot,
        )
        if compacted is None:
            run_in_scrollback(
                lambda: print_scrollback_notice(
                    scrollback_console,
                    "Nothing to compact right now.",
                )
            )
        else:
            _persist_prompt_compaction(
                compacted,
                state=state,
                state_lock=state_lock,
                agent=agent,
                active_session=active_session_ref["active_session"],
                scrollback_console=scrollback_console,
                run_in_scrollback=run_in_scrollback,
            )
        next_prompt, next_user_message, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            estimate_toolbar_context_usage=estimate_toolbar_context_usage,
        )
        if next_prompt is not None:
            persist_prompt_queue(
                active_session_ref["active_session"],
                state.pending_prompts,
                state.pending_images,
            )
            start_turn(next_prompt, next_user_message)
            return
        if should_finish:
            invalidate_prompt()
            return
        update_status("")
        invalidate_prompt()

    thread = Thread(target=run_compaction, daemon=True)
    with state_lock:
        state.worker = thread
        state.active_stop_request = None
    update_status("Compacting conversation...")
    thread.start()
    return thread


def _persist_prompt_compaction(
    compacted,
    *,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session: ActiveSession,
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
) -> None:
    (
        updated_messages,
        _preparation,
        _result,
        conversation_entries,
        compaction_payload,
        usage_payload,
    ) = compacted
    with state_lock:
        state.messages = updated_messages
        state.context_usage_text = format_context_usage_text(usage_payload)
    persist_session_state(
        active_session,
        agent,
        updated_messages,
        conversation_entries=conversation_entries,
    )
    run_in_scrollback(
        lambda: print_scrollback_notice(
            scrollback_console,
            format_compaction_note(compaction_payload),
        )
    )
