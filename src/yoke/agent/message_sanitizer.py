"""Helpers for normalizing agent message sequences."""

from __future__ import annotations

from collections.abc import Iterable

from yoke.agent.models import Message


def normalize_tool_call_sequence(
    messages: Iterable[Message], *, drop_incomplete_assistant: bool
) -> list[Message]:
    """Normalize assistant/tool sequencing while preserving later messages."""
    repaired: list[Message] = []
    pending_index: int | None = None
    pending_ids: list[str] = []
    buffered_follow_ups: list[Message] = []
    for message in messages:
        copied = message.model_copy(deep=True)
        if copied.role == "tool" and copied.tool_calls:
            copied.tool_calls = []
        if copied.role == "assistant" and copied.tool_calls:
            if pending_index is not None and drop_incomplete_assistant:
                del repaired[pending_index]
                repaired.extend(buffered_follow_ups)
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
        del repaired[pending_index]
        repaired.extend(buffered_follow_ups)
    return repaired
