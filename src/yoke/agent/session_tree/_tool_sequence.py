"""Shared inspection helpers for provider tool-call sequencing."""

from __future__ import annotations

from collections.abc import Sequence
import json

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message


def open_tool_call_ids(entries: Sequence[ConversationEntry]) -> tuple[str, ...]:
    """Return tool calls still open at the end of a message sequence."""
    open_calls: list[str] = []
    for entry in entries:
        message = entry.message
        if message is None:
            continue
        if message.role == "assistant" and message.tool_calls:
            open_calls = [call.id for call in message.tool_calls]
        elif message.role == "tool" and message.tool_call_id in open_calls:
            open_calls.remove(message.tool_call_id)
    return tuple(open_calls)


def tail_open_tool_call_ids(entries: Sequence[ConversationEntry]) -> tuple[str, ...]:
    """Return an incomplete tool batch only when it is the active tail."""
    open_calls: list[str] = []
    for entry in entries:
        message = entry.message
        if message is None:
            continue
        if message.role == "assistant" and message.tool_calls:
            if open_calls:
                return ()
            open_calls = [call.id for call in message.tool_calls]
            continue
        if not open_calls:
            continue
        if message.role == "tool" and message.tool_call_id in open_calls:
            open_calls.remove(message.tool_call_id)
            continue
        # A non-result provider message after an incomplete tool batch cannot be
        # repaired append-only. Only heal batches that are literally at the leaf.
        return ()
    return tuple(open_calls)


def recovered_tool_result_entries(
    call_ids: Sequence[str],
    *,
    parent_id: str | None,
    error: str,
) -> list[ConversationEntry]:
    """Build append-only cancellation results for an incomplete tool batch."""
    recovered: list[ConversationEntry] = []
    current_parent = parent_id
    for call_id in call_ids:
        message = Message.tool(
            call_id,
            json.dumps(
                {
                    "ok": False,
                    "cancelled": True,
                    "error": error,
                },
                separators=(",", ":"),
            ),
        )
        entry = ConversationEntry(
            kind="tool_result",
            message=message,
            parent_id=current_parent,
            metadata={"recovered_incomplete_tool_call": True},
        )
        recovered.append(entry)
        current_parent = entry.id
    return recovered
