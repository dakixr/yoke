"""Helpers for normalizing agent message sequences."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from typing import cast

from yoke.agent.models import Message


def sanitize_json_surrogates(value: dict[str, object]) -> dict[str, object]:
    """Return a JSON-safe copy with surrogate pairs normalized."""
    return cast(dict[str, object], _sanitize_json_surrogates(value))


def _sanitize_json_surrogates(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text_surrogates(value)
    if isinstance(value, Mapping):
        return {
            _sanitize_json_surrogates(key): _sanitize_json_surrogates(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_surrogates(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_json_surrogates(item) for item in value)
    return value


def _sanitize_text_surrogates(text: str) -> str:
    if not any("\ud800" <= character <= "\udfff" for character in text):
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def normalize_tool_call_sequence(
    messages: Iterable[Message],
    *,
    drop_incomplete_assistant: bool,
    drop_orphan_tool_results: bool = False,
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
                _drop_pending_incomplete_turn(
                    repaired,
                    pending_index=pending_index,
                    buffered_follow_ups=buffered_follow_ups,
                    drop_orphan_tool_results=drop_orphan_tool_results,
                )
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
        if copied.role == "tool" and drop_orphan_tool_results:
            continue
        repaired.append(copied)
    if pending_index is not None and drop_incomplete_assistant:
        _drop_pending_incomplete_turn(
            repaired,
            pending_index=pending_index,
            buffered_follow_ups=buffered_follow_ups,
            drop_orphan_tool_results=drop_orphan_tool_results,
        )
    return repaired


def _drop_pending_incomplete_turn(
    repaired: list[Message],
    *,
    pending_index: int,
    buffered_follow_ups: Iterable[Message],
    drop_orphan_tool_results: bool,
) -> None:
    if drop_orphan_tool_results:
        del repaired[pending_index:]
    else:
        del repaired[pending_index]
    repaired.extend(
        _safe_follow_ups(
            buffered_follow_ups,
            drop_orphan_tool_results=drop_orphan_tool_results,
        )
    )


def _safe_follow_ups(
    messages: Iterable[Message], *, drop_orphan_tool_results: bool
) -> list[Message]:
    if not drop_orphan_tool_results:
        return list(messages)
    return [message for message in messages if message.role != "tool"]
