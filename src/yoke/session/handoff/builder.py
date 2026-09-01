"""Build bounded portable context from persisted Yoke sessions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.store import SessionStore

from .models import DEFAULT_HANDOFF_MAX_CHARS
from .models import SessionHandoff
from .models import SessionHandoffImage
from .models import SessionHandoffMessage
from .models import SessionHandoffToolCall
from .reader import read_handoff_context_window
from .render import render_handoff_message

MIN_HANDOFF_ENTRY_LIMIT = 100
MAX_HANDOFF_ENTRY_LIMIT = 1_000
MESSAGE_TEXT_LIMIT = 60_000
TOOL_RESULT_TEXT_LIMIT = 16_000
TOOL_ARGUMENT_LIMIT = 6_000


def build_session_handoff(
    session_id: str,
    *,
    store: SessionStore | None = None,
    max_chars: int = DEFAULT_HANDOFF_MAX_CHARS,
) -> SessionHandoff:
    """Build a bounded, compaction-aware handoff without running a Yoke server."""
    if max_chars < 10_000:
        raise ValueError("max_chars must be at least 10000.")
    session_store = store or SessionStore()
    if not session_store.exists(session_id):
        raise ValueError(f"Session not found: {session_id}")

    summary = session_store.index_entry(session_id)
    record = summary.to_record() if summary is not None else None
    entries, total_entries, source_truncated, fallback_record = _context_entries(
        session_store,
        session_id,
        max_chars=max_chars,
    )
    if record is None:
        record = fallback_record or session_store.load(session_id)

    messages = _handoff_messages(entries)
    bounded, omitted_messages = _bound_messages(messages, max_chars=max_chars)
    return SessionHandoff(
        session_id=session_id,
        title=record.title,
        root=record.root,
        provider_name=record.provider_name,
        model_id=record.model_id,
        reasoning_effort=record.reasoning_effort,
        updated_at=record.updated_at,
        leaf_id=record.leaf_id,
        active_skills=[skill.name for skill in record.active_skills],
        total_entries=total_entries,
        retained_entries=len(entries),
        omitted_messages=omitted_messages,
        truncated=(
            source_truncated
            or omitted_messages > 0
            or any(message.truncated for message in bounded)
        ),
        max_chars=max_chars,
        messages=bounded,
    )


def _context_entries(
    store: SessionStore,
    session_id: str,
    *,
    max_chars: int,
) -> tuple[list[ConversationEntry], int, bool, SessionRecord | None]:
    entry_limit = min(
        MAX_HANDOFF_ENTRY_LIMIT,
        max(MIN_HANDOFF_ENTRY_LIMIT, max_chars // 1_000),
    )
    window = read_handoff_context_window(
        store,
        session_id,
        recent_limit=entry_limit,
    )
    if window is not None:
        return list(window.entries), window.total_entries, window.truncated, None

    record = store.load(session_id)
    view = SessionTree.restore(record.conversation_entries, record.leaf_id).project(
        ConversationProjection()
    )
    entries = list(view.runtime_entries)
    return entries, len(view.active_entries), False, record


def _handoff_messages(
    entries: Sequence[ConversationEntry],
) -> list[SessionHandoffMessage]:
    latest_checkpoint = next(
        (
            index
            for index in range(len(entries) - 1, -1, -1)
            if entries[index].kind == "memory_snapshot"
        ),
        None,
    )
    if latest_checkpoint is not None:
        entries = entries[latest_checkpoint:]
    result: list[SessionHandoffMessage] = []
    for entry in entries:
        if entry.kind == "instruction":
            continue
        if entry.kind == "memory_snapshot":
            _append_snapshot_messages(result, entry)
            continue
        if entry.kind == "compaction_summary":
            continue
        if entry.message is not None:
            result.append(_handoff_message(entry.message, source="conversation"))
    return result


def _append_snapshot_messages(
    result: list[SessionHandoffMessage],
    entry: ConversationEntry,
) -> None:
    try:
        snapshot = MemorySnapshot.model_validate(entry.metadata)
    except ValueError:
        snapshot = None
    if snapshot is None:
        if entry.message is not None:
            result.append(_handoff_message(entry.message, source="compaction_summary"))
        return
    handoff = snapshot.compaction_handoff
    summary_message = entry.message or Message.assistant(snapshot.summary_text)
    result.append(_handoff_message(summary_message, source="compaction_summary"))
    if handoff is not None:
        result.extend(
            _handoff_message(message, source="compaction_retained")
            for message in handoff.retained_messages
        )


def _handoff_message(
    message: Message,
    *,
    source: Literal["conversation", "compaction_summary", "compaction_retained"],
) -> SessionHandoffMessage:
    content, images = _message_content(message)
    text_limit = (
        TOOL_RESULT_TEXT_LIMIT if message.role == "tool" else MESSAGE_TEXT_LIMIT
    )
    content, content_truncated = _truncate_text(content, text_limit)
    tool_calls: list[SessionHandoffToolCall] = []
    for call in message.tool_calls:
        arguments, arguments_truncated = _truncate_text(
            call.function.arguments,
            TOOL_ARGUMENT_LIMIT,
        )
        tool_calls.append(
            SessionHandoffToolCall(
                id=call.id,
                name=call.function.name,
                arguments=arguments,
                truncated=arguments_truncated,
            )
        )
    return SessionHandoffMessage(
        role=message.role,
        content=content,
        phase=message.phase,
        tool_call_id=message.tool_call_id,
        tool_calls=tool_calls,
        images=images,
        source=source,
        truncated=content_truncated or any(call.truncated for call in tool_calls),
    )


def _message_content(message: Message) -> tuple[str, list[SessionHandoffImage]]:
    if isinstance(message.content, str):
        return message.content, []
    if message.content is None:
        return "", []
    text: list[str] = []
    images: list[SessionHandoffImage] = []
    for part in message.content:
        if isinstance(part, MessageTextContentPart):
            if part.text:
                text.append(part.text)
            continue
        if isinstance(part, MessageLocalImageContentPart):
            images.append(
                SessionHandoffImage(label=part.display_label, source=part.path)
            )
            continue
        if isinstance(part, MessageImageURLContentPart):
            url = part.image_url.url
            images.append(
                SessionHandoffImage(
                    label=part.display_label,
                    source=(
                        url
                        if not url.startswith("data:") and len(url) <= 2_000
                        else None
                    ),
                )
            )
    return "\n".join(text), images


def _bound_messages(
    messages: Sequence[SessionHandoffMessage],
    *,
    max_chars: int,
) -> tuple[list[SessionHandoffMessage], int]:
    if not messages:
        return [], 0
    body_budget = max(2_000, max_chars - 4_000)
    selected: dict[int, SessionHandoffMessage] = {}
    used = 0
    summary_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].source == "compaction_summary"
        ),
        None,
    )
    if summary_index is not None:
        summary_budget = min(30_000, max(1_500, body_budget // 2))
        summary = _fit_message_to_budget(messages[summary_index], summary_budget)
        selected[summary_index] = summary
        used = min(body_budget, len("\n".join(render_handoff_message(summary))) + 2)

    for index in range(len(messages) - 1, -1, -1):
        if index == summary_index:
            continue
        remaining = body_budget - used
        if remaining <= 0:
            break
        message = messages[index]
        cost = len("\n".join(render_handoff_message(message))) + 2
        if cost <= remaining:
            selected[index] = message
            used += cost
            continue
        if index == len(messages) - 1:
            selected[index] = _fit_message_to_budget(message, remaining)
        break

    if not selected:
        selected[len(messages) - 1] = _fit_message_to_budget(
            messages[-1],
            body_budget,
        )

    bounded = [selected[index] for index in sorted(selected)]
    return bounded, len(messages) - len(bounded)


def _fit_message_to_budget(
    message: SessionHandoffMessage,
    budget: int,
) -> SessionHandoffMessage:
    candidate = message.model_copy(deep=True)
    candidate.truncated = True
    budget = max(500, budget)
    rendered_cost = len("\n".join(render_handoff_message(candidate))) + 2
    if rendered_cost <= budget:
        return candidate

    excess = rendered_cost - budget
    if candidate.content:
        target = max(200, len(candidate.content) - excess - 200)
        candidate.content, _ = _truncate_text(candidate.content, target)
        rendered_cost = len("\n".join(render_handoff_message(candidate))) + 2
    if rendered_cost <= budget:
        return candidate

    if candidate.tool_calls:
        remaining = max(200, budget // max(1, len(candidate.tool_calls)))
        for call in candidate.tool_calls:
            call.arguments, _ = _truncate_text(
                call.arguments,
                min(len(call.arguments), remaining),
            )
            call.truncated = True
    return candidate


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = f"\n\n[... {len(value) - limit:,} characters omitted ...]\n\n"
    available = max(0, limit - len(marker))
    head = (available * 2) // 3
    tail = available - head
    return f"{value[:head]}{marker}{value[-tail:] if tail else ''}", True
