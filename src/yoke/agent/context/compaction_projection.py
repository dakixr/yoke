"""Cheap projections that preserve established compaction runtime semantics."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.session_tree import InvalidCheckpointError


def compacted_runtime_messages(
    context: AgentContext,
    *,
    kept_messages: Sequence[Message],
    summary_message: Message,
) -> list[Message]:
    """Rebuild post-compaction runtime messages without re-projecting history."""
    return [
        *[message.model_copy(deep=True) for message in context.instructions],
        *_active_skill_messages(
            context.conversation_log.entries,
            leaf_id=context.conversation_log.leaf_id,
        ),
        *[message.model_copy(deep=True) for message in kept_messages],
        summary_message.model_copy(deep=True),
    ]


def _active_skill_messages(
    entries: Sequence[ConversationEntry],
    *,
    leaf_id: str | None,
) -> list[Message]:
    if leaf_id is None:
        return []
    by_id = {entry.id: entry for entry in entries}
    selected: list[Message] = []
    current: str | None = leaf_id
    while current is not None:
        entry = by_id[current]
        if entry.kind == "skill_event" and entry.message is not None:
            selected.append(entry.message.model_copy(deep=True))
        current = entry.parent_id
    selected.reverse()
    return selected


def next_compaction_generation_from_active_path(context: AgentContext) -> int:
    """Return the next checkpoint generation without projecting message history."""
    entries = context.conversation_log.entries
    by_id = {entry.id: entry for entry in entries}
    current = context.conversation_log.leaf_id
    while current is not None:
        entry = by_id[current]
        if entry.kind == "memory_snapshot":
            try:
                snapshot = MemorySnapshot.model_validate(entry.metadata)
            except ValueError:
                raise InvalidCheckpointError(
                    f"Applicable memory snapshot {entry.id[:8]!r} is invalid."
                ) from None
            handoff = snapshot.compaction_handoff
            if handoff is not None:
                return handoff.generation + 1
            generation = snapshot.metadata.get("generation")
            return generation + 1 if isinstance(generation, int) else 2
        current = entry.parent_id
    return 1
