"""Prompt turn cancellation and checkpoint helpers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock

from yoke.agent.models import ConversationEntry, Message
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.runtime import ActiveSession
from yoke.cli.backend import SessionBackend


def retire_active_turn(
    *,
    state: PromptCliState,
    active_session: ActiveSession,
    stop_event: Event,
    status_message: str,
    retire_tool_traces: Callable[[int], None],
) -> tuple[list[Message], list[ConversationEntry]]:
    """Fence an active turn and return its synthetic continuation state."""
    abandoned_turn_ids, _ = (state.abandoned_turn_ids, state.steered_turn_ids)
    SessionBackend.interrupt(stop_event)
    abandoned_turn_ids.add(state.active_turn_id)
    retire_tool_traces(state.active_turn_id)
    messages, entries = SessionBackend.interrupt_snapshot(
        messages=state.messages,
        entries=active_session.active_entry_refs(),
        user_message=state.active_user_message,
    )
    state.messages = messages
    state.continuation_entries = entries
    state.worker = None
    state.active_stop_request = None
    state.active_user_message = None
    state.status_message = status_message
    return messages, entries


def persist_stopped_turn_if_idle(
    *,
    state: PromptCliState,
    state_lock: Lock,
    retired_turn_id: int,
    active_session: ActiveSession,
    agent: object,
    messages: list[Message],
    entries: list[ConversationEntry],
) -> None:
    """Persist an instant stop unless another generation already started."""
    with active_session.save_lock:
        with state_lock:
            if state.active_turn_id != retired_turn_id or state.worker is not None:
                return
        SessionBackend.wrap(agent, active_session).persist(
            messages,
            conversation_entries=entries,
        )
