"""Helper functions for context reconstruction and persistence."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.conversation import (
    memory_message_has_continuation_note,
    parse_memory_message,
    project_conversation,
)
from yoke.agent.interruptions import (
    INTERRUPTED_TURN_NOTICE as INTERRUPTED_TURN_NOTICE,
    is_interruption_notice,
)
from yoke.agent.models import (
    AgentContext,
    ConversationEntry,
    ConversationEntryKind,
    ConversationLog,
    MemorySnapshot,
    Message,
)
from yoke.agent.session_tree import SessionTree
from yoke.agent.state import active_branch_entries


def append_conversation_entry(context: AgentContext, entry: ConversationEntry) -> bool:
    """Append an entry at the active leaf and return whether it branches."""
    entries = context.conversation_log.entries
    tree = SessionTree.borrow_validated(
        entries,
        context.conversation_log.leaf_id,
    )
    branching = bool(entries and tree.leaf_id != entries[-1].id)
    cursor = len(entries)
    _append_entry_intent(tree, entry)
    delta = tree.export_append_delta(cursor)
    entries.extend(delta.entries)
    context.conversation_log.leaf_id = delta.leaf_id
    return branching


def append_control_message(
    context: AgentContext,
    message: Message,
    *,
    metadata: dict[str, object],
) -> None:
    """Append a model-visible internal event outside the chat transcript."""
    copied = message.model_copy(deep=True)
    branching = append_conversation_entry(
        context,
        ConversationEntry(kind="control", message=copied, metadata=metadata),
    )
    update_message_projection(context, copied, branching=branching)


def _append_entry_intent(tree: SessionTree, entry: ConversationEntry) -> None:
    if entry.kind == "skill_event" and entry.message is not None:
        tree.append_system_event(entry.message, metadata=entry.metadata)
        return
    if entry.kind == "control":
        tree.append_control(entry.metadata, message=entry.message)
        return
    if entry.kind == "compaction_summary":
        tree.append_compaction_attempt(entry.metadata, message=entry.message)
        return
    if entry.kind == "memory_snapshot":
        tree.append_snapshot(
            MemorySnapshot.model_validate(entry.metadata),
            message=entry.message,
        )
        return
    if entry.kind == "branch_summary" and entry.message is not None:
        tree.append_branch_summary(entry.message, metadata=entry.metadata)
        return
    if entry.kind == "tool_context" and entry.message is not None:
        tree.append_tool_context(entry.message, metadata=entry.metadata)
        return
    if entry.message is None:
        raise ValueError(f"Entry kind {entry.kind!r} requires a message.")
    if entry.message.role == "system":
        tree.append_system_event(entry.message, metadata=entry.metadata)
        return
    tree.append_message(entry.message, metadata=entry.metadata)


def update_message_projection(
    context: AgentContext, message: Message, *, branching: bool
) -> None:
    """Update the transcript projection after one message append."""
    if not branching:
        context.messages.append(message.model_copy(deep=True))
        return
    entries = active_branch_entries(
        context.conversation_log.entries,
        leaf_id=context.conversation_log.leaf_id,
    )
    context.messages = [
        instruction.model_copy(deep=True) for instruction in context.instructions
    ]
    context.messages.extend(
        _runtime_message(entry)
        for entry in entries or []
        if entry.kind != "memory_snapshot" and entry.message is not None
    )


def _runtime_message(entry: ConversationEntry) -> Message:
    """Return one runtime message with non-persisted projection provenance."""
    assert entry.message is not None  # noqa: S101
    message = entry.message.model_copy(deep=True)
    if entry.kind == "tool_result":
        projection = entry.metadata.get("provider_result_projection")
        message._provider_result_projection = (
            projection if isinstance(projection, str) and projection else None
        )
    return message


def resolve_instructions(
    messages: Sequence[Message],
    instructions: Sequence[Message],
) -> list[Message]:
    """Resolve leading instruction messages for the context."""
    if instructions:
        return [message.model_copy(deep=True) for message in instructions]
    leading_system: list[Message] = []
    for message in messages:
        if message.role != "system" or is_interruption_notice(message):
            break
        leading_system.append(message.model_copy(deep=True))
    if leading_system:
        return leading_system
    return [message.model_copy(deep=True) for message in instructions]


def build_conversation_log(
    messages: Sequence[Message],
    memory_snapshot: MemorySnapshot | None,
) -> ConversationLog:
    """Build a conversation log from persisted transcript messages."""
    tree = SessionTree.from_runtime_messages(messages, memory_snapshot)
    exported = tree.export()
    return ConversationLog(
        entries=list(exported.entries),
        leaf_id=exported.leaf_id,
    )


def build_conversation_log_from_entries(
    entries: Sequence[ConversationEntry],
    memory_snapshot: MemorySnapshot | None,
) -> ConversationLog:
    """Rebuild a conversation log from stored ConversationEntry values."""
    tree = SessionTree.restore_runtime(entries, memory_snapshot)
    exported = tree.export()
    return ConversationLog(
        entries=list(exported.entries),
        leaf_id=exported.leaf_id,
    )


def entry_kind_for_message(message: Message) -> ConversationEntryKind:
    """Map a transcript message to a persisted entry kind."""
    if is_interruption_notice(message):
        return "control"
    if message.role == "user":
        return "user"
    if message.role == "tool":
        return "tool_result"
    if message.role == "assistant" and message.tool_calls:
        return "assistant_tool_calls"
    if message.role == "assistant":
        return "assistant"
    return "instruction"


def extract_persisted_memory_snapshot(
    messages: Sequence[Message],
) -> MemorySnapshot | None:
    """Extract the latest persisted memory snapshot from transcript messages."""
    for message in messages:
        plain_text = message.plain_text_content
        if message.role not in {"system", "user"} or not plain_text:
            continue
        parsed = parse_memory_message(plain_text)
        if parsed is not None:
            return MemorySnapshot(
                id="memory-current",
                summary_text=parsed,
                metadata={"mid_turn": memory_message_has_continuation_note(plain_text)},
            )
    return None


def extract_memory_snapshot_from_entries(
    entries: Sequence[ConversationEntry],
) -> MemorySnapshot | None:
    """Extract the latest memory snapshot from stored entries."""
    return project_conversation(entries).checkpoint


def strip_persisted_memory_messages(
    messages: Sequence[Message],
) -> list[Message]:
    """Remove persisted memory messages from the transcript view."""
    stripped: list[Message] = []
    for message in messages:
        plain_text = message.plain_text_content
        if message.role in {"system", "user"} and plain_text:
            if parse_memory_message(plain_text) is not None:
                continue
        stripped.append(message.model_copy(deep=True))
    return stripped


def normalize_instructions(
    instructions: Sequence[Message] | None,
) -> list[Message]:
    """Normalize system instruction messages."""
    normalized: list[Message] = []
    for message in instructions or []:
        if message.role != "system":
            raise ValueError("Instructions must all have role='system'")
        normalized.append(message.model_copy(deep=True))
    return normalized
