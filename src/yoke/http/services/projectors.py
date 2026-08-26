"""Stable public projections over Yoke session-tree values."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.http.models.session import AssistantProjectedMessage
from yoke.http.models.session import ControlProjectedMessage
from yoke.http.models.session import ImageContent
from yoke.http.models.session import ProjectedContent
from yoke.http.models.session import ProjectedMessage
from yoke.http.models.session import TextContent
from yoke.http.models.session import ToolCallSummary
from yoke.http.models.session import ToolProjectedMessage
from yoke.http.models.session import TreeEntryInfo
from yoke.http.models.session import UserProjectedMessage


def project_active_messages(record_entries: list[ConversationEntry], leaf_id: str | None) -> list[ProjectedMessage]:
    """Project the selected branch without exposing provider-private fields."""
    tree = SessionTree.restore(record_entries, leaf_id)
    view = tree.project(ConversationProjection())
    return [project_entry(entry) for entry in view.active_entries if _is_public_entry(entry)]


def project_tree(record_entries: list[ConversationEntry], leaf_id: str | None) -> list[TreeEntryInfo]:
    """Project the full tree into lightweight inspector rows."""
    SessionTree.restore(record_entries, leaf_id)
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
        TreeEntryInfo(
            id=entry.id,
            parent_id=entry.parent_id,
            kind=entry.kind,
            created_at=entry.created_at,
            label=_entry_label(entry),
            active=entry.id in active_ids,
            current=entry.id == leaf_id,
            preview=_entry_preview(entry),
            child_count=child_counts[entry.id],
        )
        for entry in record_entries
    ]


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
            content=_content(message),
        )
    if entry.kind in {"assistant", "assistant_tool_calls"}:
        return AssistantProjectedMessage(
            id=entry.id,
            time_created=entry.created_at,
            kind=entry.kind,
            phase=message.phase if message is not None else None,
            content=_content(message),
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
            call_id=message.tool_call_id if message is not None else None,
            result=message.display_text_content() if message is not None else None,
        )
    return ControlProjectedMessage(
        id=entry.id,
        time_created=entry.created_at,
        kind=entry.kind,
        control=entry.kind,
        text=_entry_preview(entry),
    )


def _is_public_entry(entry: ConversationEntry) -> bool:
    return entry.kind not in {"instruction", "memory_snapshot"}


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
            content.append(ImageContent(name=part.display_label, uri=part.image_url.url))
    return content


_content = project_message_content


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
