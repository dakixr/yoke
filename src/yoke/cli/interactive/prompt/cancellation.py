"""Prompt turn cancellation and checkpoint helpers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event

from yoke.agent.loop import INTERRUPTED_TURN_NOTICE
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import prompt_turn_tracking
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime import persist_session_state
from yoke.cli.session import SessionTreeIndex


def retire_active_turn(
    *,
    state: PromptCliState,
    active_session: ActiveSession,
    stop_event: Event,
    status_message: str,
    retire_tool_traces: Callable[[int], None],
) -> tuple[list[Message], list[ConversationEntry]]:
    """Fence an active turn and return its synthetic continuation state."""
    abandoned_turn_ids, _ = prompt_turn_tracking(state)
    stop_event.set()
    abandoned_turn_ids.add(state.active_turn_id)
    retire_tool_traces(state.active_turn_id)
    messages, entries = interrupted_turn_snapshot(
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
    retired_turn_id: int,
    active_session: ActiveSession,
    agent: AgentRunner,
    messages: list[Message],
    entries: list[ConversationEntry],
) -> None:
    """Persist an instant stop unless another generation already started."""
    with active_session.save_lock:
        if state.active_turn_id != retired_turn_id or state.worker is not None:
            return
        persist_session_state(
            active_session,
            agent,
            messages,
            conversation_entries=entries,
        )


def interrupted_turn_snapshot(
    *,
    messages: list[Message],
    entries: list[ConversationEntry],
    user_message: Message | None,
    leaf_id: str | None = None,
) -> tuple[list[Message], list[ConversationEntry]]:
    """Create a continuation checkpoint without waiting for a retired turn."""
    active = active_branch_entry_refs(entries, leaf_id=leaf_id)
    snapshot_messages = list(messages)
    snapshot_entries = list(active)
    parent_id = active[-1].id if active else None
    if user_message is not None:
        copied_user = user_message.model_copy(deep=True)
        user_entry = ConversationEntry(
            kind="user",
            message=copied_user.model_copy(deep=True),
            parent_id=parent_id,
        )
        snapshot_messages.append(copied_user)
        snapshot_entries.append(user_entry)
        parent_id = user_entry.id
    interruption = Message.assistant(INTERRUPTED_TURN_NOTICE)
    snapshot_messages.append(interruption)
    snapshot_entries.append(
        ConversationEntry(
            kind="assistant",
            message=interruption.model_copy(deep=True),
            parent_id=parent_id,
        )
    )
    return snapshot_messages, snapshot_entries


def active_branch_entry_refs(
    entries: list[ConversationEntry],
    *,
    leaf_id: str | None,
) -> list[ConversationEntry]:
    """Adapt persisted active entries while preserving existing references."""
    return list(SessionTreeIndex(entries, leaf_id).active_entry_refs())
