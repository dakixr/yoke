"""Storage-agnostic agent state capture and hydration primitives."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.session_tree import ConversationProjection
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


class AgentState(BaseModel):
    """Portable structured state captured from an agent."""

    conversation_entries: list[ConversationEntry] | None = None
    active_skills: list[ActiveSkill] | None = None
    skill_dirs: list[str] | None = None

    @property
    def messages(self) -> list[Message]:
        """Transcript projection derived from structured conversation state."""
        return transcript_messages_from_entries(self.conversation_entries)


def capture_agent_state(
    agent: object,
    *,
    messages: Sequence[Message] | None = None,
    conversation_entries: Sequence[ConversationEntry] | None = None,
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
        active_skills=_agent_active_skills(agent),
        skill_dirs=_agent_skill_dirs(agent),
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
    load_conversation(
        conversation_entries=state.conversation_entries,
        available_skills=available_skills,
        active_skills=state.active_skills,
    )


def transcript_messages_from_entries(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
) -> list[Message]:
    """Return transcript messages from canonical conversation entries."""
    if entries is None:
        return []
    projection = SessionTree.restore(entries, leaf_id=leaf_id).project(
        ConversationProjection()
    )
    return [message.model_copy(deep=True) for message in projection.transcript_messages]


def conversation_entries_from_messages(
    messages: Sequence[Message] | None,
) -> list[ConversationEntry]:
    """Build canonical conversation entries from legacy message history."""
    valid_messages = [
        message for message in messages or [] if isinstance(message, Message)
    ]
    return list(SessionTree.from_messages(valid_messages).entries)


def active_branch_entries(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
) -> list[ConversationEntry] | None:
    """Return entries on the active branch from root to leaf."""
    if entries is None:
        return None
    projection = SessionTree.restore(entries, leaf_id=leaf_id).project(
        ConversationProjection()
    )
    return [entry.model_copy(deep=True) for entry in projection.runtime_entries]


def _active_branch_entry_refs(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
) -> list[ConversationEntry] | None:
    """Return internal references on the active branch."""
    return active_branch_entries(entries, leaf_id=leaf_id)


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
