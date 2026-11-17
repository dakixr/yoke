"""Cheap projections that preserve established compaction runtime semantics."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.compaction.render import USER_MESSAGE_TRUNCATION_SUFFIX
from yoke.agent.state import active_branch_entries
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


def compaction_user_entries(context: AgentContext) -> list[ConversationEntry]:
    """Select actual user entries, respecting the last checkpoint's retention.

    Handoffs store message values without entry kinds. Match retained values to
    their source user entries so legacy internal notices cannot become prompts.
    Preserve truncated values rather than restoring the omitted prompt text.
    """
    selected: list[ConversationEntry] = []
    for entry in (
        active_branch_entries(
            context.conversation_log.entries, leaf_id=context.conversation_log.leaf_id
        )
        or []
    ):
        if entry.kind == "user" and entry.message is not None:
            selected.append(entry)
        elif entry.kind == "memory_snapshot":
            snapshot = MemorySnapshot.model_validate(entry.metadata)
            handoff = snapshot.compaction_handoff
            if handoff is None:
                continue
            if not handoff.retained_messages and handoff.retained_user_messages:
                continue  # Legacy checkpoints did not store retained values.
            retained: list[ConversationEntry] = []
            for message in handoff.retained_messages:
                for source in selected:
                    if _retained_user_value(message, source.message):
                        retained.append(source.model_copy(update={"message": message}))
                        break
            selected = retained
    return selected


def _retained_user_value(retained: Message, original: Message | None) -> bool:
    if original is None or retained.role != "user":
        return False
    if retained == original:
        return True
    text = retained.plain_text_content or ""
    if not text.endswith(USER_MESSAGE_TRUNCATION_SUFFIX):
        return False
    snippet = text.removesuffix(USER_MESSAGE_TRUNCATION_SUFFIX)
    return bool(snippet) and (original.text_content() or "").endswith(snippet)
