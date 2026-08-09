"""Intent-level mutation implementation for SessionTree."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from yoke.agent.compaction.types import CompactionBoundary
from yoke.agent.compaction.types import CompactionReason
from yoke.agent.models import CompactionHandoff
from yoke.agent.models import ConversationEntry
from yoke.agent.models import ConversationEntryKind
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill

from .errors import InvalidCheckpointError
from .errors import InvalidMessageError
from .errors import InvalidToolSequenceError
from .values import EntryRef
from ._topology import active_path


class SessionTreeMutations:
    """Implement SessionTree mutation intents behind its public interface."""

    _entries: list[ConversationEntry]
    _leaf_id: str | None
    _scope: str

    def append_message(self, message: Message) -> EntryRef:
        """Append one user, assistant, or tool message."""
        if not isinstance(message, Message):
            raise TypeError("append_message requires a Message value.")
        if message.role == "system":
            raise InvalidMessageError("System messages must use append_system_event().")
        _validate_role_tool_fields(message)
        self._validate_tool_sequence(message)
        return self._append_entry(
            _kind_for_message(message),
            message=message,
            metadata=_message_metadata(message),
        )

    def _append_imported_message(self, message: Message) -> EntryRef:
        return self._append_entry(
            _kind_for_message(message),
            message=message,
            metadata=_message_metadata(message),
        )

    def append_system_event(
        self,
        message: Message,
        *,
        metadata: dict[str, object] | None = None,
    ) -> EntryRef:
        """Append a rendered skill or system activation event."""
        if not isinstance(message, Message) or message.role != "system":
            raise InvalidMessageError("A system event requires role='system'.")
        return self._append_entry("skill_event", message=message, metadata=metadata)

    def append_active_skills(
        self, skills: Sequence[ActiveSkill]
    ) -> tuple[EntryRef, ...]:
        """Append rendered activation events for the supplied active skills."""
        from yoke.agent.prompting import render_active_skill_message

        appended: list[EntryRef] = []
        for skill in skills:
            if not isinstance(skill, ActiveSkill):
                raise TypeError("append_active_skills requires ActiveSkill values.")
            activation_id = skill.activation_id or (
                f"legacy:{skill.name}:{skill.source_path}"
            )
            appended.append(
                self.append_system_event(
                    render_active_skill_message(skill),
                    metadata={
                        "skill_name": skill.name,
                        "skill_activation_id": activation_id,
                    },
                )
            )
        return tuple(appended)

    def append_control(
        self,
        metadata: dict[str, object],
        *,
        message: Message | None = None,
    ) -> EntryRef:
        """Append an audit-only control fact."""
        return self._append_entry("control", message=message, metadata=metadata)

    def append_compaction_attempt(
        self,
        metadata: dict[str, object],
        *,
        message: Message | None = None,
    ) -> EntryRef:
        """Append one compaction-attempt audit fact."""
        return self._append_entry(
            "compaction_summary", message=message, metadata=metadata
        )

    def append_snapshot(
        self, snapshot: MemorySnapshot, *, message: Message | None = None
    ) -> EntryRef:
        """Append one prepared memory snapshot."""
        if not isinstance(snapshot, MemorySnapshot):
            raise InvalidCheckpointError(
                "append_snapshot requires a MemorySnapshot value."
            )
        return self._append_entry(
            "memory_snapshot", message=message, metadata=snapshot.model_dump()
        )

    def append_branch_summary(
        self,
        message: Message,
        *,
        metadata: dict[str, object] | None = None,
    ) -> EntryRef:
        """Append a branch summary."""
        if message.role != "assistant":
            raise InvalidMessageError("A branch summary requires role='assistant'.")
        return self._append_entry("branch_summary", message=message, metadata=metadata)

    def append_checkpoint(
        self,
        summary_text: str,
        *,
        retained_messages: Sequence[Message] = (),
        mid_turn: bool = False,
        reason: CompactionReason = "manual",
        boundary: CompactionBoundary = "user",
        input_tokens: int | None = None,
        total_tokens: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> EntryRef:
        """Atomically append a summary marker and checkpoint."""
        if self._leaf_id is None:
            raise InvalidCheckpointError(
                "A checkpoint requires at least one covered entry."
            )
        if not isinstance(summary_text, str) or not summary_text.strip():
            raise InvalidCheckpointError("Checkpoint summary text is required.")
        retained = _copy_retained_messages(retained_messages)
        generation = self._next_checkpoint_generation()
        handoff = CompactionHandoff(
            summary_text=summary_text,
            reason=reason,
            boundary=boundary,
            summarized_messages=len(self._active_entries()),
            retained_user_messages=sum(message.role == "user" for message in retained),
            retained_messages=retained,
            generation=generation,
            input_tokens=input_tokens,
            total_tokens=total_tokens,
        )
        snapshot_metadata = deepcopy(metadata or {})
        snapshot_metadata.update({"generation": generation, "mid_turn": mid_turn})
        snapshot = MemorySnapshot(
            id=f"memory-{generation}",
            summary_text=summary_text,
            compaction_handoff=handoff,
            metadata=snapshot_metadata,
        )
        summary = self._new_entry("compaction_summary", parent_id=self._leaf_id)
        checkpoint = self._new_entry(
            "memory_snapshot",
            parent_id=summary.id,
            metadata=snapshot.model_dump(),
        )
        self._entries.extend((summary, checkpoint))
        self._leaf_id = checkpoint.id
        return EntryRef(self._scope, checkpoint.id)

    def _append_entry(
        self,
        kind: ConversationEntryKind,
        *,
        message: Message | None = None,
        metadata: dict[str, object] | None = None,
    ) -> EntryRef:
        entry = self._new_entry(
            kind,
            parent_id=self._leaf_id,
            message=message,
            metadata=metadata,
        )
        self._entries.append(entry)
        self._leaf_id = entry.id
        return EntryRef(self._scope, entry.id)

    @staticmethod
    def _new_entry(
        kind: ConversationEntryKind,
        *,
        parent_id: str | None,
        message: Message | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ConversationEntry:
        return ConversationEntry(
            kind=kind,
            parent_id=parent_id,
            message=(message.model_copy(deep=True) if message is not None else None),
            metadata=deepcopy(metadata or {}),
        )

    def _active_entries(self) -> list[ConversationEntry]:
        return active_path(self._entries, self._leaf_id)

    def _validate_tool_sequence(self, message: Message) -> None:
        open_calls: set[str] = set()
        for entry in self._active_entries():
            current = entry.message
            if current is None:
                continue
            if current.role == "assistant" and current.tool_calls:
                open_calls = {call.id for call in current.tool_calls}
            elif current.role == "tool" and current.tool_call_id in open_calls:
                open_calls.remove(current.tool_call_id)
        if message.role == "tool":
            if not message.tool_call_id or message.tool_call_id not in open_calls:
                raise InvalidToolSequenceError(
                    "Tool result does not match an open tool call."
                )
        elif open_calls:
            raise InvalidToolSequenceError(
                "All open tool calls require results before another message."
            )
        if message.tool_calls:
            ids = [call.id for call in message.tool_calls]
            if any(not item for item in ids) or len(ids) != len(set(ids)):
                raise InvalidToolSequenceError(
                    "Assistant tool-call identifiers must be unique and non-empty."
                )

    def _next_checkpoint_generation(self) -> int:
        generations = [
            value
            for entry in self._entries
            if entry.kind == "memory_snapshot"
            for value in [_generation_from_entry(entry)]
            if value is not None
        ]
        return max(generations, default=0) + 1


def _kind_for_message(message: Message) -> ConversationEntryKind:
    if message.role == "user":
        return "user"
    if message.role == "tool":
        return "tool_result"
    if message.role == "assistant" and message.tool_calls:
        return "assistant_tool_calls"
    return "assistant"


def _validate_role_tool_fields(message: Message) -> None:
    if message.role == "user" and (
        message.tool_call_id is not None or message.tool_calls
    ):
        raise InvalidToolSequenceError("User messages cannot contain tool-call fields.")
    if message.role == "assistant" and message.tool_call_id is not None:
        raise InvalidToolSequenceError(
            "Assistant messages cannot contain a tool result identifier."
        )
    if message.role == "tool" and message.tool_calls:
        raise InvalidToolSequenceError("Tool results cannot create new tool calls.")


def _generation_from_entry(entry: ConversationEntry) -> int | None:
    try:
        snapshot = MemorySnapshot.model_validate(entry.metadata)
    except ValueError:
        return None
    if snapshot.compaction_handoff is not None:
        return snapshot.compaction_handoff.generation
    value = snapshot.metadata.get("generation")
    return value if isinstance(value, int) and value > 0 else 1


def _message_metadata(message: Message) -> dict[str, object]:
    from yoke.agent.usage import compact_usage_payload

    usage = compact_usage_payload(message.usage)
    return {"usage": usage} if usage is not None else {}


def _copy_retained_messages(
    messages: Sequence[Message],
) -> list[Message]:
    retained: list[Message] = []
    for message in messages:
        if not isinstance(message, Message):
            raise InvalidCheckpointError("Retained checkpoint values must be messages.")
        retained.append(message.model_copy(deep=True))
    return retained
