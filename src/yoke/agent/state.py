"""Storage-agnostic agent state capture and hydration primitives."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import secrets

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from yoke.agent.models import ConversationEntryKind
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.loop import ConversationEntryHistory
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.usage import compact_usage_payload


class AgentState(BaseModel):
    """Portable structured state captured from an agent."""

    conversation_entries: list[ConversationEntry] = Field(default_factory=list)
    leaf_id: str | None = None
    active_skills: list[ActiveSkill] = Field(default_factory=list)
    skill_dirs: list[str] = Field(default_factory=list)

    @field_validator(
        "conversation_entries",
        "active_skills",
        "skill_dirs",
        mode="before",
    )
    @classmethod
    def normalize_legacy_null_collections(cls, value: object) -> object:
        """Normalize legacy persisted null collections at the input boundary."""
        return [] if value is None else value

    @property
    def messages(self) -> list[Message]:
        """Transcript projection derived from structured conversation state."""
        return transcript_messages_from_entries(
            self.conversation_entries,
            leaf_id=self.leaf_id,
        )


def capture_agent_state(
    agent: object,
    *,
    messages: Sequence[Message] | None = None,
    conversation_entries: Sequence[ConversationEntry] | None = None,
    leaf_id: str | None = None,
) -> AgentState:
    """Capture structured state from an agent-like object.

    Structured conversation entries take precedence over transcript messages
    because they preserve memory snapshots and compaction handoffs.
    """
    resolved_entries = _copy_conversation_entries(
        conversation_entries
        if conversation_entries is not None
        else _agent_conversation_entries(agent)
    )
    if resolved_entries is None:
        resolved_entries = conversation_entries_from_messages(
            messages if messages is not None else _agent_messages(agent)
        )
    return AgentState(
        conversation_entries=resolved_entries,
        leaf_id=(
            leaf_id
            if leaf_id is not None
            else (resolved_entries[-1].id if resolved_entries else None)
        ),
        active_skills=_agent_active_skills(agent) or [],
        skill_dirs=_agent_skill_dirs(agent) or [],
    )


def hydrate_agent_state(
    agent: object,
    state: AgentState,
    *,
    available_skills: Sequence[SkillSpec] | None = None,
) -> None:
    """Hydrate an agent-like object from structured state."""
    load_conversation = getattr(agent, "load_conversation", None)
    if not callable(load_conversation):
        raise TypeError("Agent does not support structured state hydration.")
    active_entries = active_branch_entries(
        state.conversation_entries,
        leaf_id=state.leaf_id,
    )
    load_conversation(
        ConversationEntryHistory(active_entries or []),
        available_skills=available_skills,
        active_skills=state.active_skills,
    )


def transcript_messages_from_entries(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
) -> list[Message]:
    """Return transcript messages from canonical conversation entries."""
    active_entries = active_branch_entries(entries, leaf_id=leaf_id)
    if active_entries is None:
        return []
    return [
        entry.message.model_copy(deep=True)
        for entry in active_entries
        if entry.message is not None and entry.kind != "memory_snapshot"
    ]


def conversation_entries_from_messages(
    messages: Sequence[Message] | None,
) -> list[ConversationEntry]:
    """Build canonical conversation entries from legacy message history."""
    entries: list[ConversationEntry] = []
    parent_id: str | None = None
    for message in _copy_messages(messages):
        entry = ConversationEntry(
            kind=_entry_kind_for_message(message),
            message=message.model_copy(deep=True),
            parent_id=parent_id,
            metadata=_message_entry_metadata(message),
        )
        entries.append(entry)
        parent_id = entry.id
    return entries


def migrate_conversation_tree(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
    assume_linear: bool = False,
) -> tuple[list[ConversationEntry], str | None, bool]:
    """Return entries with tree ids/parents and a valid active leaf."""
    migrated: list[ConversationEntry] = []
    changed = False
    parent_id: str | None = None
    seen_ids: set[str] = set()
    for entry in entries or []:
        copied = entry.model_copy(deep=True)
        if not copied.id or copied.id in seen_ids:
            copied.id = secrets.token_hex(8)
            changed = True
        seen_ids.add(copied.id)
        if assume_linear and copied.parent_id is None and migrated:
            copied.parent_id = parent_id
            changed = True
        if copied.parent_id == copied.id:
            copied.parent_id = None
            changed = True
        if copied.parent_id is not None and copied.parent_id not in seen_ids:
            copied.parent_id = parent_id
            changed = True
        migrated.append(copied)
        parent_id = copied.id
    valid_leaf_id = leaf_id if leaf_id in seen_ids else parent_id
    if leaf_id != valid_leaf_id:
        changed = True
    return migrated, valid_leaf_id, changed


def active_branch_entries(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
) -> list[ConversationEntry] | None:
    """Return entries on the active branch from root to leaf."""
    if entries is None:
        return None
    migrated, resolved_leaf_id, _changed = migrate_conversation_tree(
        entries,
        leaf_id=leaf_id,
    )
    if resolved_leaf_id is None:
        return []
    by_id = {entry.id: entry for entry in migrated}
    path: list[ConversationEntry] = []
    current_id: str | None = resolved_leaf_id
    seen: set[str] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            break
        path.append(entry.model_copy(deep=True))
        current_id = entry.parent_id
    path.reverse()
    return path


def merge_conversation_branch(
    existing_entries: Sequence[ConversationEntry],
    branch_entries: Sequence[ConversationEntry],
) -> tuple[list[ConversationEntry], str | None]:
    """Merge an updated active branch back into the full tree."""
    merged, _existing_leaf_id, _changed = migrate_conversation_tree(existing_entries)
    by_id = {entry.id: index for index, entry in enumerate(merged)}
    leaf_id: str | None = None
    for entry in branch_entries:
        copied = entry.model_copy(deep=True)
        leaf_id = copied.id
        if copied.id in by_id:
            merged[by_id[copied.id]] = copied
            continue
        by_id[copied.id] = len(merged)
        merged.append(copied)
    if leaf_id is None and merged:
        leaf_id = merged[-1].id
    return merged, leaf_id


def _agent_conversation_entries(
    agent: object,
) -> Sequence[ConversationEntry] | None:
    value = getattr(agent, "conversation_entries", None)
    if isinstance(value, Sequence):
        return value
    return None


def _agent_messages(agent: object) -> Sequence[Message] | None:
    value = getattr(agent, "messages", None)
    if isinstance(value, Sequence):
        return value
    return None


def _agent_active_skills(agent: object) -> list[ActiveSkill] | None:
    value = getattr(agent, "active_skills", None)
    if not isinstance(value, Sequence):
        return None
    return [
        skill.model_copy(deep=True) for skill in value if isinstance(skill, ActiveSkill)
    ]


def _agent_skill_dirs(agent: object) -> list[str] | None:
    registry = getattr(agent, "skill_registry", None)
    skills = getattr(registry, "skills", None)
    if not isinstance(skills, Sequence):
        return None
    paths: set[str] = set()
    for skill in skills:
        root = getattr(skill, "root", None)
        if isinstance(root, Path):
            paths.add(str(root.parent))
    return sorted(paths)


def _copy_messages(messages: Sequence[Message] | None) -> list[Message]:
    if messages is None:
        return []
    return [
        message.model_copy(deep=True)
        for message in messages
        if isinstance(message, Message)
    ]


def _copy_conversation_entries(
    entries: Sequence[ConversationEntry] | None,
) -> list[ConversationEntry] | None:
    if entries is None:
        return None
    return [
        entry.model_copy(deep=True)
        for entry in entries
        if isinstance(entry, ConversationEntry)
    ]


def _entry_kind_for_message(message: Message) -> ConversationEntryKind:
    if message.role == "user":
        return "user"
    if message.role == "tool":
        return "tool_result"
    if message.role == "assistant" and message.tool_calls:
        return "assistant_tool_calls"
    if message.role == "assistant":
        return "assistant"
    return "instruction"


def _message_entry_metadata(message: Message) -> dict[str, object]:
    usage = compact_usage_payload(message.usage)
    if usage is None:
        return {}
    return {"usage": usage}
