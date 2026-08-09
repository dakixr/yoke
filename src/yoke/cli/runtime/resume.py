"""Resume projections for runtime hydration and human-readable replay."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.session_tree import ConversationProjection
from yoke.agent.session_tree import parse_memory_message
from yoke.agent.session_tree import ScrollbackProjection
from yoke.agent.session_tree import SessionTree
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionTreeIndex

RESUME_SCROLLBACK_MESSAGE_LIMIT = 400


@dataclass(frozen=True, slots=True)
class ResumeProjection:
    """Prepared views of one persisted session's active branch."""

    active_entries: list[ConversationEntry]
    runtime_messages: list[Message]
    scrollback_messages: list[Message]
    scrollback_notice: str | None


def project_resumed_session(
    record: SessionRecord,
    *,
    tree_index: SessionTreeIndex | None = None,
) -> ResumeProjection:
    """Project full runtime state and a generous bounded scrollback view."""
    index = tree_index or SessionTreeIndex(
        record.conversation_entries,
        record.leaf_id,
    )
    active = list(index.active_entry_refs())
    source = (
        record.conversation_entries
        if _needs_detached_handoff_recovery(active)
        else active
    )
    tree = SessionTree.restore(
        source,
        leaf_id=record.leaf_id,
    )
    projection = tree.project(ConversationProjection())
    entries = [entry.model_copy(deep=True) for entry in projection.runtime_entries]
    runtime_messages = [
        message.model_copy(deep=True) for message in projection.runtime_messages
    ]
    scrollback = tree.project(
        ScrollbackProjection(limit=RESUME_SCROLLBACK_MESSAGE_LIMIT)
    )
    return ResumeProjection(
        active_entries=entries,
        runtime_messages=runtime_messages,
        scrollback_messages=[message.to_message() for message in scrollback.messages],
        scrollback_notice=scrollback.notice,
    )


def _needs_detached_handoff_recovery(
    active: list[ConversationEntry],
) -> bool:
    return any(
        entry.message is not None
        and entry.message.role == "user"
        and parse_memory_message(entry.message.plain_text_content or "") is not None
        for entry in active
    )
