"""Prompt-toolkit compaction worker control."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock, Thread

from yoke.cli.interactive.common import PendingPrompt, PromptCliState
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.render import format_compaction_note
from yoke.cli.runtime import ActiveSession, AgentRunner
from yoke.cli.runtime import force_compact_history
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import session_usage_metric_context


def start_prompt_compaction(
    *,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session_ref: dict[str, ActiveSession],
    scrollback: ScrollbackSink,
    request_context_usage: Callable[[str], None],
    update_status: Callable[[str], None],
    start_pending_prompt: Callable[[PendingPrompt | None, bool], None],
) -> Thread:
    """Run forced compaction in the active-worker slot."""
    scrollback.emit("notice", "Compacting conversation...")
    with state_lock:
        message_snapshot = list(state.messages)
        current_session = active_session_ref["active_session"]
    conversation_entries_snapshot = current_session.active_entries()

    def run_compaction() -> None:
        with session_usage_metric_context(current_session, ""):
            compacted = force_compact_history(
                agent,
                message_snapshot,
                conversation_entries=conversation_entries_snapshot,
            )
        if compacted is None:
            scrollback.emit("notice", "Nothing to compact right now.")
        else:
            _persist_prompt_compaction(
                compacted,
                state=state,
                state_lock=state_lock,
                agent=agent,
                active_session=active_session_ref["active_session"],
                scrollback=scrollback,
            )
        pending, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            active_session=active_session_ref["active_session"],
            request_context_usage=request_context_usage,
        )
        start_pending_prompt(pending, should_finish)

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
    scrollback: ScrollbackSink,
) -> None:
    (
        updated_messages,
        _preparation,
        _result,
        conversation_entries,
        compaction_payload,
        _usage_payload,
    ) = compacted
    with state_lock:
        state.messages = updated_messages
    persist_session_state(
        active_session,
        agent,
        updated_messages,
        conversation_entries=conversation_entries,
    )
    scrollback.emit("notice", format_compaction_note(compaction_payload))
