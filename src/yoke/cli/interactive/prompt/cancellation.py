"""Prompt turn cancellation and display fencing."""

from __future__ import annotations

from yoke.session.interrupt import interrupted_turn_snapshot

from collections.abc import Callable
from threading import Event

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.backend.daemon import DaemonActiveSession


def retire_active_turn(
    *,
    state: PromptCliState,
    active_session: DaemonActiveSession,
    stop_event: Event,
    status_message: str,
    retire_tool_traces: Callable[[int], None],
) -> None:
    """Fence local rendering while the daemon persists the interruption."""
    abandoned_turn_ids, _ = (state.abandoned_turn_ids, state.steered_turn_ids)
    stop_event.set()
    active_session.backend.interrupt()
    abandoned_turn_ids.add(state.active_turn_id)
    retire_tool_traces(state.active_turn_id)
    messages, _entries = interrupted_turn_snapshot(
        messages=state.messages,
        entries=active_session.active_entry_refs(),
        user_message=state.active_user_message,
    )
    state.messages = messages
    state.worker = None
    state.active_stop_request = None
    state.active_user_message = None
    state.status_message = status_message
