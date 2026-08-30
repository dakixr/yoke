"""Shared logical interruption checkpoints for resumable session turns."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.loop import INTERRUPTED_TURN_NOTICE
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.session_tree._tool_sequence import recovered_tool_result_entries
from yoke.agent.session_tree._tool_sequence import tail_open_tool_call_ids
from yoke.session import SessionTreeIndex


def interrupted_turn_snapshot(
    *,
    messages: Sequence[Message],
    entries: Sequence[ConversationEntry],
    user_message: Message | None,
    leaf_id: str | None = None,
) -> tuple[list[Message], list[ConversationEntry]]:
    """Create a continuation checkpoint without waiting for retired work."""
    active = active_branch_entries(entries, leaf_id=leaf_id)
    snapshot_messages = list(messages)
    snapshot_entries = list(active)
    parent_id = active[-1].id if active else None
    dangling_call_ids = tail_open_tool_call_ids(active)
    if dangling_call_ids:
        recovered = recovered_tool_result_entries(
            dangling_call_ids,
            parent_id=parent_id,
            error="Tool call cancelled because the turn was interrupted.",
        )
        snapshot_entries.extend(recovered)
        snapshot_messages.extend(
            entry.message for entry in recovered if entry.message is not None
        )
        parent_id = recovered[-1].id
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


def active_branch_entries(
    entries: Sequence[ConversationEntry],
    *,
    leaf_id: str | None,
) -> list[ConversationEntry]:
    """Return borrowed active-path entries while preserving existing references."""
    return list(SessionTreeIndex(entries, leaf_id).active_entry_refs())
