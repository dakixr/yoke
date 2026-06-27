"""Helpers for normalizing agent message sequences."""

from __future__ import annotations

import json
from collections.abc import Iterable

from yoke.agent.models import Message

INTERNAL_TOOL_RESULT_KEYS = frozenset({"context_messages"})


def sanitize_tool_result_payload(result: dict[str, object]) -> dict[str, object]:
    """Return a provider-visible tool result without internal control fields."""
    if not any(key in result for key in INTERNAL_TOOL_RESULT_KEYS):
        return dict(result)
    return {
        key: value
        for key, value in result.items()
        if key not in INTERNAL_TOOL_RESULT_KEYS
    }


def sanitize_tool_result_message(message: Message) -> Message:
    """Strip internal control fields from JSON tool-result messages."""
    copied = message.model_copy(deep=True)
    if copied.role != "tool" or not isinstance(copied.content, str):
        return copied
    try:
        payload = json.loads(copied.content)
    except json.JSONDecodeError:
        return copied
    if not isinstance(payload, dict):
        return copied
    sanitized = sanitize_tool_result_payload(payload)
    if sanitized == payload:
        return copied
    copied.content = json.dumps(sanitized, ensure_ascii=False)
    return copied


def normalize_tool_call_sequence(
    messages: Iterable[Message], *, drop_incomplete_assistant: bool
) -> list[Message]:
    """Normalize assistant/tool sequencing while preserving later messages."""
    repaired: list[Message] = []
    pending_index: int | None = None
    pending_ids: list[str] = []
    buffered_follow_ups: list[Message] = []

    def drop_pending_turn() -> None:
        nonlocal pending_index, pending_ids, buffered_follow_ups
        if pending_index is not None:
            del repaired[pending_index:]
            repaired.extend(
                message for message in buffered_follow_ups if message.role != "tool"
            )
        pending_index = None
        pending_ids = []
        buffered_follow_ups = []

    for message in messages:
        copied = message.model_copy(deep=True)
        if copied.role == "tool" and copied.tool_calls:
            copied.tool_calls = []
        if copied.role == "assistant" and copied.tool_calls:
            if pending_index is not None and drop_incomplete_assistant:
                drop_pending_turn()
            pending_index = len(repaired)
            pending_ids = [tool_call.id for tool_call in copied.tool_calls]
            buffered_follow_ups = []
            repaired.append(copied)
            continue
        if pending_index is not None:
            if (
                copied.role == "tool"
                and pending_ids
                and copied.tool_call_id == pending_ids[0]
            ):
                repaired.append(copied)
                pending_ids.pop(0)
                if not pending_ids:
                    pending_index = None
                    buffered_follow_ups = []
                continue
            buffered_follow_ups.append(copied)
            continue
        repaired.append(copied)
    if pending_index is not None and drop_incomplete_assistant:
        drop_pending_turn()
    return repaired
