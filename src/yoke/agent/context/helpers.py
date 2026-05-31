"""Helper functions for context reconstruction and persistence."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import ConversationEntryKind
from yoke.agent.models import ConversationLog
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.models import WorkingMemory
from yoke.agent.prompting import memory_message_has_continuation_note
from yoke.agent.prompting import parse_memory_message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


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
        persisted_entries = [
            entry.model_copy(deep=True) for entry in conversation_entries
        ]
        persisted_messages = [
            entry.message.model_copy(deep=True)
            for entry in persisted_entries
            if entry.message is not None and entry.kind != "instruction"
        ]
        resolved_instructions = resolve_instructions(persisted_messages, instructions)
        prior_memory_snapshot = extract_memory_snapshot_from_entries(persisted_entries)
        conversation_log = build_conversation_log_from_entries(
            persisted_entries,
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
        memory=WorkingMemory(current_snapshot=prior_memory_snapshot),
        available_skills=[
            skill.model_copy(deep=True) for skill in available_skills or []
        ],
        active_skills=[skill.model_copy(deep=True) for skill in active_skills or []],
    )
    if append_prompt:
        append_message(context, user_message or Message.user(prompt))
    else:
        context.messages = transcript_messages(context)
    return context


def recent_log_messages(context: AgentContext) -> list[Message]:
    """Return recent non-instruction, non-snapshot conversation messages."""
    messages: list[Message] = []
    for entry in context.conversation_log.entries:
        if entry.kind in {"instruction", "memory_snapshot"}:
            continue
        if entry.message is not None:
            messages.append(entry.message.model_copy(deep=True))
    return messages


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
    entries: list[ConversationEntry] = []
    stripped_messages = strip_persisted_memory_messages(messages)
    for message in resolve_instructions(stripped_messages, instructions):
        entries.append(ConversationEntry(kind="instruction", message=message))
    memory_snapshot_added = False
    for message in messages:
        plain_text = message.plain_text_content
        if message.role in {"system", "user"} and plain_text:
            if parse_memory_message(plain_text) is not None:
                if memory_snapshot is not None:
                    entries.append(
                        ConversationEntry(
                            kind="memory_snapshot",
                            metadata=memory_snapshot.model_dump(),
                        )
                    )
                    memory_snapshot_added = True
                continue
        if message.role == "system":
            continue
        entries.append(
            ConversationEntry(
                kind=entry_kind_for_message(message),
                message=message.model_copy(deep=True),
            )
        )
    if memory_snapshot is not None and not memory_snapshot_added:
        entries.insert(
            len(resolve_instructions(stripped_messages, instructions)),
            ConversationEntry(
                kind="memory_snapshot",
                metadata=memory_snapshot.model_dump(),
            ),
        )
    return ConversationLog(entries=entries)


def build_conversation_log_from_entries(
    entries: Sequence[ConversationEntry],
    instructions: Sequence[Message],
    memory_snapshot: MemorySnapshot | None,
) -> ConversationLog:
    """Rebuild a conversation log from stored ConversationEntry values."""
    rebuilt_entries = [
        ConversationEntry(
            kind="instruction",
            message=message.model_copy(deep=True),
        )
        for message in instructions
    ]
    for entry in entries:
        if entry.kind != "instruction":
            rebuilt_entries.append(entry.model_copy(deep=True))
    if memory_snapshot is not None and not any(
        entry.kind == "memory_snapshot" for entry in rebuilt_entries
    ):
        rebuilt_entries.insert(
            len(instructions),
            ConversationEntry(
                kind="memory_snapshot",
                metadata=memory_snapshot.model_dump(),
            ),
        )
    return ConversationLog(entries=rebuilt_entries)


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
    snapshot: MemorySnapshot | None = None
    for entry in entries:
        if entry.kind != "memory_snapshot":
            continue
        try:
            snapshot = MemorySnapshot.model_validate(entry.metadata)
        except ValueError:
            continue
    return snapshot


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
    snapshot = context.memory.current_snapshot
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
