"""Prompt-toolkit compaction worker control."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock, Thread

import httpx

from yoke.cli.interactive.common import PendingPrompt, PromptCliState
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.render import format_compaction_note
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.backend.values import ActiveSessionRef


def start_prompt_compaction(
    *,
    state: PromptCliState,
    state_lock: Lock,
    agent: DaemonSessionBackend,
    active_session_ref: ActiveSessionRef,
    scrollback: ScrollbackSink,
    request_context_usage: Callable[[str], None],
    update_status: Callable[[str], None],
    start_pending_prompt: Callable[[PendingPrompt | None, bool], None],
) -> Thread:
    """Run forced compaction in the active-worker slot."""
    scrollback.emit("notice", "Compacting conversation...")
    with state_lock:
        message_snapshot = list(state.messages)
        current_session = active_session_ref.value
    conversation_entries_snapshot = current_session.active_entries()

    def run_compaction() -> None:
        try:
            compacted = agent.compact(
                message_snapshot,
                conversation_entries=conversation_entries_snapshot,
            )
            if compacted is None:
                scrollback.emit("notice", "Nothing to compact right now.")
            else:
                with state_lock:
                    state.messages = compacted.messages
                scrollback.emit("notice", format_compaction_note(compacted.payload))
        except (ValueError, OSError, httpx.HTTPError) as exc:
            scrollback.emit("error", f"Compaction failed: {exc}")
        pending, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            active_session=active_session_ref.value,
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
