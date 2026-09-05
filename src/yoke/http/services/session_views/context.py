"""Established HTTP Context rendering for indexed windows and saved records."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.cli.session.models import SessionRecord
from yoke.http.models.session import ContextData
from yoke.http.models.session import ContextMessage
from yoke.http.models.session import ContextResponse
from yoke.http.services.projectors import project_message_content
from yoke.http.services.session_message_index.models import ContextIndexWindow


def project_indexed_context(
    window: ContextIndexWindow,
    *,
    include_system: bool,
    include_tool_results: bool,
    max_chars: int,
) -> ContextResponse:
    """Render the original bounded index-window approximation."""
    source_messages: list[Message] = []
    for entry in window.entries:
        if entry.kind == "instruction":
            if include_system and entry.message is not None:
                source_messages.append(entry.message)
            continue
        if entry.kind == "memory_snapshot":
            try:
                snapshot = MemorySnapshot.model_validate(entry.metadata)
            except ValueError:
                continue
            handoff = snapshot.compaction_handoff
            if handoff is not None:
                source_messages.extend(handoff.retained_messages)
            source_messages.append(
                entry.message or Message.assistant(snapshot.summary_text)
            )
            continue
        if entry.kind == "compaction_summary":
            continue
        if entry.message is not None:
            source_messages.append(entry.message)
    projected = _project_messages(
        source_messages,
        include_system=include_system,
        include_tool_results=include_tool_results,
    )
    bounded, retained_chars, chars_truncated = _bound_messages(
        projected,
        max_chars=max_chars,
    )
    return ContextResponse(
        data=ContextData(
            messages=bounded,
            total_entries=window.total_entries,
            retained_entries=len(window.entries),
            retained_chars=retained_chars,
            max_chars=max_chars,
            truncated=window.truncated or chars_truncated,
        )
    )


def project_saved_context(
    record: SessionRecord,
    *,
    include_system: bool,
    include_tool_results: bool,
    limit: int,
    max_chars: int,
) -> ContextResponse:
    """Render fallback Context through the canonical saved-tree projection."""
    view = SessionTree.restore(
        record.conversation_entries,
        record.leaf_id,
    ).project(ConversationProjection())
    source_messages = list(view.provider_messages)
    if include_system:
        instructions = [
            entry.message
            for entry in view.active_entries
            if entry.kind == "instruction" and entry.message is not None
        ]
        source_messages = [*instructions, *source_messages]
    total_entries = len(view.active_entries)
    if limit and len(source_messages) > limit:
        source_messages = source_messages[-limit:]
    projected = _project_messages(
        source_messages,
        include_system=include_system,
        include_tool_results=include_tool_results,
    )
    bounded, retained_chars, chars_truncated = _bound_messages(
        projected,
        max_chars=max_chars,
    )
    return ContextResponse(
        data=ContextData(
            messages=bounded,
            total_entries=total_entries,
            retained_entries=min(total_entries, limit),
            retained_chars=retained_chars,
            max_chars=max_chars,
            truncated=total_entries > limit or chars_truncated,
        )
    )


def _project_messages(
    source_messages: Sequence[Message],
    *,
    include_system: bool,
    include_tool_results: bool,
) -> list[ContextMessage]:
    messages: list[ContextMessage] = []
    for message in source_messages:
        if message.role == "system" and not include_system:
            continue
        if message.role == "tool" and not include_tool_results:
            continue
        messages.append(
            ContextMessage(
                role=message.role,
                content=project_message_content(message),
                tool_call_id=message.tool_call_id,
                phase=message.phase,
            )
        )
    return messages


def _bound_messages(
    messages: Sequence[ContextMessage],
    *,
    max_chars: int,
) -> tuple[list[ContextMessage], int, bool]:
    retained: list[ContextMessage] = []
    remaining = max_chars
    truncated = False
    for message in reversed(messages):
        text_chars = sum(
            len(part.text) for part in message.content if part.type == "text"
        )
        if text_chars <= remaining:
            retained.append(message)
            remaining -= text_chars
            continue
        if remaining <= 0:
            truncated = True
            break
        content = []
        for part in message.content:
            if part.type != "text":
                content.append(part)
                continue
            if remaining <= 0:
                continue
            if len(part.text) <= remaining:
                content.append(part)
                remaining -= len(part.text)
                continue
            content.append(part.model_copy(update={"text": part.text[:remaining]}))
            remaining = 0
            truncated = True
        retained.append(message.model_copy(update={"content": content}))
        break
    if len(retained) < len(messages):
        truncated = True
    retained.reverse()
    return retained, max_chars - remaining, truncated
