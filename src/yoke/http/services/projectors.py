"""Stable public projections over Yoke session-tree values."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.session_tree import SessionTree
from yoke.agent.tool_context import legacy_tool_context_entry_ids
from yoke.http.models.session import AssistantProjectedMessage
from yoke.http.models.session import ControlProjectedMessage
from yoke.http.models.session import ImageContent
from yoke.http.models.session import ProjectedContent
from yoke.http.models.session import ProjectedMessage
from yoke.http.models.session import TextContent
from yoke.http.models.session import ToolCallSummary
from yoke.http.models.session import TurnSummaryInfo
from yoke.http.models.session import ToolProjectedMessage
from yoke.http.models.session import TreeEntryInfo
from yoke.http.models.session import UserProjectedMessage
from yoke.session.admissions import INPUT_ID_METADATA_KEY


PUBLIC_CHAT_EXCLUDED_KINDS = frozenset(
    {"instruction", "memory_snapshot", "tool_context"}
)


def is_public_chat_entry(entry: ConversationEntry) -> bool:
    """Return whether one canonical entry belongs in the public chat view."""
    return entry.kind not in PUBLIC_CHAT_EXCLUDED_KINDS


def public_chat_entries(
    entries: Sequence[ConversationEntry],
) -> list[ConversationEntry]:
    """Apply the public chat policy, including legacy tool-context detection."""
    legacy_tool_context = legacy_tool_context_entry_ids(entries)
    return [
        entry
        for entry in entries
        if is_public_chat_entry(entry) and entry.id not in legacy_tool_context
    ]


def project_tree(
    record_entries: list[ConversationEntry], leaf_id: str | None
) -> list[TreeEntryInfo]:
    """Project the full tree into lightweight inspector rows."""
    SessionTree.restore(record_entries, leaf_id)
    legacy_tool_context = legacy_tool_context_entry_ids(record_entries)
    by_id = {entry.id: entry for entry in record_entries}
    child_counts = {entry.id: 0 for entry in record_entries}
    for entry in record_entries:
        if entry.parent_id in child_counts:
            child_counts[entry.parent_id] += 1
    active_ids: set[str] = set()
    current = leaf_id
    while current is not None and current in by_id:
        active_ids.add(current)
        current = by_id[current].parent_id
    return [
        project_tree_entry(
            entry,
            leaf_id=leaf_id,
            active=entry.id in active_ids,
            child_count=child_counts[entry.id],
            kind_override=("tool_context" if entry.id in legacy_tool_context else None),
        )
        for entry in record_entries
    ]


def project_tree_entry(
    entry: ConversationEntry,
    *,
    leaf_id: str | None,
    active: bool,
    child_count: int,
    kind_override: str | None = None,
) -> TreeEntryInfo:
    """Project one tree row when topology state is already indexed."""
    return TreeEntryInfo(
        id=entry.id,
        parent_id=entry.parent_id,
        kind=kind_override or entry.kind,
        phase=entry.message.phase if entry.message is not None else None,
        created_at=entry.created_at,
        label=_entry_label(entry),
        active=active,
        current=entry.id == leaf_id,
        preview=_entry_preview(entry),
        child_count=child_count,
    )


def _entry_label(entry: ConversationEntry) -> str | None:
    value = entry.metadata.get("label")
    return value if isinstance(value, str) else None


def project_entry(entry: ConversationEntry) -> ProjectedMessage:
    """Convert one conversation entry into a public tagged message."""
    message = entry.message
    if entry.kind == "user":
        return UserProjectedMessage(
            id=entry.id,
            time_created=entry.created_at,
            kind=entry.kind,
            turn_summary=_entry_turn_summary(entry),
            input_id=_entry_input_id(entry),
            content=project_message_content(message),
        )
    if entry.kind in {"assistant", "assistant_tool_calls"}:
        return AssistantProjectedMessage(
            id=entry.id,
            time_created=entry.created_at,
            kind=entry.kind,
            turn_summary=_entry_turn_summary(entry),
            phase=message.phase if message is not None else None,
            content=project_message_content(message),
            tool_calls=[
                ToolCallSummary(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
                for call in (message.tool_calls if message is not None else [])
            ],
        )
    if entry.kind == "tool_result":
        return ToolProjectedMessage(
            id=entry.id,
            time_created=entry.created_at,
            kind=entry.kind,
            turn_summary=_entry_turn_summary(entry),
            call_id=message.tool_call_id if message is not None else None,
            result=message.display_text_content() if message is not None else None,
        )
    return ControlProjectedMessage(
        id=entry.id,
        time_created=entry.created_at,
        kind=entry.kind,
        turn_summary=_entry_turn_summary(entry),
        control=entry.kind,
        text=_entry_preview(entry),
    )


def project_message_content(message: Message | None) -> list[ProjectedContent]:
    """Project message content without exposing local file paths."""
    if message is None or message.content is None:
        return []
    if isinstance(message.content, str):
        return [TextContent(text=message.content)] if message.content else []
    content: list[ProjectedContent] = []
    for part in message.content:
        if isinstance(part, MessageTextContentPart):
            content.append(TextContent(text=part.text))
        elif isinstance(part, MessageLocalImageContentPart):
            content.append(ImageContent(name=Path(part.path).name))
        elif isinstance(part, MessageImageURLContentPart):
            content.append(
                ImageContent(name=part.display_label, uri=part.image_url.url)
            )
    return content


def _entry_input_id(entry: ConversationEntry) -> str | None:
    value = entry.metadata.get(INPUT_ID_METADATA_KEY)
    return value if isinstance(value, str) else None


def _entry_turn_summary(entry: ConversationEntry) -> TurnSummaryInfo | None:
    value = entry.metadata.get("yoke_turn_summary")
    if not isinstance(value, dict):
        return None
    duration = value.get("duration_seconds")
    tool_count = value.get("tool_count")
    if not isinstance(duration, int | float) or isinstance(duration, bool):
        return None
    if not isinstance(tool_count, int) or isinstance(tool_count, bool):
        return None
    if duration < 0 or tool_count < 0:
        return None
    return TurnSummaryInfo(duration_seconds=float(duration), tool_count=tool_count)


def _entry_preview(entry: ConversationEntry) -> str | None:
    if entry.message is not None:
        text = entry.message.display_text_content()
        if text:
            return _truncate(text)
    for key in ("summary_text", "text", "reason"):
        value = entry.metadata.get(key)
        if isinstance(value, str) and value:
            return _truncate(value)
    return None


def _truncate(value: str, limit: int = 160) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."
