"""Provenance helpers for provider-visible messages injected by tools."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart


TOOL_CONTEXT_TOOL_NAMES = frozenset({"attach_image", "image_generation"})


def message_has_image(message: Message | None) -> bool:
    """Return whether a message contains an image attachment."""
    if message is None or not isinstance(message.content, list):
        return False
    return any(
        isinstance(part, MessageImageURLContentPart | MessageLocalImageContentPart)
        for part in message.content
    )


def legacy_tool_context_entry_ids(
    entries: Sequence[ConversationEntry],
) -> set[str]:
    """Identify pre-provenance tool-injected image messages conservatively."""
    by_id = {entry.id: entry for entry in entries}
    result: set[str] = set()
    for entry in entries:
        if entry.kind != "user" or not message_has_image(entry.message):
            continue
        parent = by_id.get(entry.parent_id or "")
        if (
            parent is None
            or parent.kind != "tool_result"
            or parent.message is None
            or not parent.message.tool_call_id
        ):
            continue
        if (
            _ancestor_tool_name(
                by_id,
                parent.parent_id,
                parent.message.tool_call_id,
            )
            in TOOL_CONTEXT_TOOL_NAMES
        ):
            result.add(entry.id)
    return result


def normalize_legacy_tool_context_entries(
    entries: Sequence[ConversationEntry],
) -> set[str]:
    """Reclassify legacy tool-injected image entries in-place."""
    legacy_ids = legacy_tool_context_entry_ids(entries)
    if not legacy_ids:
        return legacy_ids
    by_id = {entry.id: entry for entry in entries}
    for entry_id in legacy_ids:
        entry = by_id[entry_id]
        parent = by_id.get(entry.parent_id or "")
        call_id = parent.message.tool_call_id if parent and parent.message else None
        tool_name = (
            _ancestor_tool_name(by_id, parent.parent_id, call_id)
            if parent is not None and call_id
            else None
        )
        entry.kind = "tool_context"
        if tool_name:
            entry.metadata.setdefault("tool_name", tool_name)
        if call_id:
            entry.metadata.setdefault("tool_call_id", call_id)
        entry.metadata.setdefault("legacy_tool_context", True)
    return legacy_ids


def _ancestor_tool_name(
    by_id: dict[str, ConversationEntry],
    current_id: str | None,
    call_id: str,
) -> str | None:
    seen: set[str] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            return None
        if entry.kind == "user":
            return None
        if entry.kind == "assistant_tool_calls" and entry.message is not None:
            for call in entry.message.tool_calls:
                if call.id == call_id:
                    return call.function.name
        current_id = entry.parent_id
    return None
