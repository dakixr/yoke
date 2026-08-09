"""Helper functions for context reconstruction and persistence."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.conversation import project_conversation
from yoke.agent.conversation import memory_message_has_continuation_note
from yoke.agent.conversation import parse_memory_message
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import ConversationEntryKind
from yoke.agent.models import ConversationLog
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.state import _active_branch_entry_refs


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
    if entry.message is None:
        raise ValueError(f"Entry kind {entry.kind!r} requires a message.")
    if entry.message.role == "system":
        tree.append_system_event(entry.message, metadata=entry.metadata)
        return
    tree.append_message(entry.message)


def update_message_projection(
    context: AgentContext, message: Message, *, branching: bool
) -> None:
    """Update the transcript projection after one message append."""
    if not branching:
        context.messages.append(message.model_copy(deep=True))
        return
    entries = _active_branch_entry_refs(
        context.conversation_log.entries,
        leaf_id=context.conversation_log.leaf_id,
    )
    context.messages = [
        instruction.model_copy(deep=True) for instruction in context.instructions
    ]
    context.messages.extend(
        entry.message.model_copy(deep=True)
        for entry in entries or []
        if entry.kind != "memory_snapshot" and entry.message is not None
    )


def initialize_context_state(
    *,
    prompt: str,
    messages: list[Message] | None,
    instructions: list[Message],
    system_prompt: str | None,
    user_message: Message | None,
    append_prompt: bool,
    conversation_entries: Sequence[ConversationEntry] | None,
    available_skills: Sequence[SkillSpec] | None,
    active_skills: Sequence[ActiveSkill] | None,
    append_message,
    transcript_messages,
) -> AgentContext:
    """Build the initial AgentContext state from persisted messages/entries."""
    if conversation_entries is not None:
        persisted_messages = [
            entry.message
            for entry in conversation_entries
            if entry.message is not None and entry.kind != "instruction"
        ]
        resolved_instructions = resolve_instructions(persisted_messages, instructions)
        prior_memory_snapshot = extract_memory_snapshot_from_entries(
            conversation_entries
        )
        conversation_log = build_conversation_log_from_entries(
            conversation_entries,
            resolved_instructions,
            prior_memory_snapshot,
        )
    else:
        persisted_messages = [
            message.model_copy(deep=True) for message in messages or []
        ]
        prior_memory_snapshot = extract_persisted_memory_snapshot(persisted_messages)
        recent_messages = strip_persisted_memory_messages(persisted_messages)
        resolved_instructions = resolve_instructions(recent_messages, instructions)
        conversation_log = build_conversation_log(
            persisted_messages,
            prior_memory_snapshot,
            instructions=resolved_instructions,
        )
    context = AgentContext(
        system_prompt=system_prompt,
        messages=[],
        instructions=resolved_instructions,
        conversation_log=conversation_log,
        available_skills=[
            skill.model_copy(deep=True) for skill in available_skills or []
        ],
        active_skills=[skill.model_copy(deep=True) for skill in active_skills or []],
    )
    context.messages = transcript_messages(context)
    if append_prompt:
        append_message(context, user_message or Message.user(prompt))
    return context


def initialize_owned_context_state(
    *,
    prompt: str,
    entries: list[ConversationEntry],
    instructions: list[Message],
    system_prompt: str | None,
    user_message: Message | None,
    append_prompt: bool,
    available_skills: Sequence[SkillSpec] | None,
    active_skills: Sequence[ActiveSkill] | None,
    append_message,
) -> AgentContext:
    """Take a validated active path without recopying its entry values."""
    persisted_messages = [
        entry.message
        for entry in entries
        if entry.message is not None and entry.kind != "instruction"
    ]
    resolved_instructions = resolve_instructions(persisted_messages, instructions)
    seed = SessionTree.take_validated_runtime(entries)
    context = AgentContext.model_construct(
        system_prompt=system_prompt,
        messages=[*resolved_instructions, *seed.messages],
        instructions=resolved_instructions,
        conversation_log=ConversationLog.model_construct(
            entries=seed.entries,
            leaf_id=seed.leaf_id,
        ),
        available_skills=[
            skill.model_copy(deep=True) for skill in available_skills or []
        ],
        active_skills=[skill.model_copy(deep=True) for skill in active_skills or []],
        provider_epoch_reset=False,
    )
    if append_prompt:
        append_message(context, user_message or Message.user(prompt))
    return context


def recent_log_messages(context: AgentContext) -> list[Message]:
    """Return recent non-instruction, non-snapshot conversation messages."""
    projection = project_conversation(
        context.conversation_log.entries,
        leaf_id=context.conversation_log.leaf_id,
    )
    return [message.model_copy(deep=True) for message in projection.provider_messages]


def resolve_instructions(
    messages: Sequence[Message],
    instructions: Sequence[Message],
) -> list[Message]:
    """Resolve leading instruction messages for the context."""
    if instructions:
        return [message.model_copy(deep=True) for message in instructions]
    leading_system: list[Message] = []
    for message in messages:
        if message.role != "system":
            break
        leading_system.append(message.model_copy(deep=True))
    if leading_system:
        return leading_system
    return [message.model_copy(deep=True) for message in instructions]


def build_conversation_log(
    messages: Sequence[Message],
    memory_snapshot: MemorySnapshot | None,
    *,
    instructions: Sequence[Message],
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
    instructions: Sequence[Message],
    memory_snapshot: MemorySnapshot | None,
) -> ConversationLog:
    """Rebuild a conversation log from stored ConversationEntry values."""
    del instructions
    tree = SessionTree.restore_runtime(entries, memory_snapshot)
    exported = tree.export()
    return ConversationLog(
        entries=list(exported.entries),
        leaf_id=exported.leaf_id,
    )


def entry_kind_for_message(message: Message) -> ConversationEntryKind:
    """Map a transcript message to a persisted entry kind."""
    if message.role == "user":
        return "user"
    if message.role == "tool":
        return "tool_result"
    if message.role == "assistant" and message.tool_calls:
        return "assistant_tool_calls"
    if (
        message.role == "assistant"
        and message.plain_text_content == INTERRUPTED_TURN_NOTICE
    ):
        return "control"
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


def next_compaction_generation(context: AgentContext) -> int:
    """Compute the next compaction generation number."""
    snapshot = project_conversation(
        context.conversation_log.entries,
        leaf_id=context.conversation_log.leaf_id,
    ).checkpoint
    if snapshot is None:
        return 1
    if snapshot.compaction_handoff is not None:
        return snapshot.compaction_handoff.generation + 1
    current = snapshot.metadata.get("generation")
    if isinstance(current, int):
        return current + 1
    return 2


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


INTERRUPTED_TURN_NOTICE = (
    "The previous turn was interrupted by the user before completion. Continue "
    "from the current state and follow the user's next instruction."
)
