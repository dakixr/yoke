"""Record fixtures for direct SDK and storage tests, without terminal execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.state import (
    AgentState,
    capture_agent_state,
    conversation_entries_from_messages,
    transcript_messages_from_entries,
)
from yoke.cli.providers.state import (
    capture_provider_session_state,
    provider_session_state_from_values,
)
from yoke.session import SessionStore
from yoke.session.repository import SessionRecord, SessionTreeIndex


@dataclass(slots=True)
class StoredSession:
    """A retained storage record and active-path index for SDK fixtures."""

    id: str
    root: Path
    store: SessionStore
    record: SessionRecord
    title: str | None = None
    save_lock: RLock = field(default_factory=RLock, repr=False)
    tree_index: SessionTreeIndex = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and index loaded topology once for this session lifetime."""
        self.tree_index = SessionTreeIndex(
            self.record.conversation_entries,
            self.record.leaf_id,
        )

    def active_entry_refs(self) -> list[ConversationEntry]:
        """Return a shallow active-path snapshot for internal runtime use."""
        return list(self.tree_index.active_entry_refs())

    def active_entries(self) -> list[ConversationEntry]:
        """Return defensive active-path entries for an ownership transfer."""
        return self.tree_index.active_entries()

    def messages(self) -> list[Message]:
        """Project transcript messages from the retained active path."""
        return [
            entry.message.model_copy(deep=True)
            for entry in self.tree_index.active_entry_refs()
            if entry.kind not in {"instruction", "memory_snapshot"}
            and entry.message is not None
        ]


def save_active_session(
    active_session: StoredSession,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None = None,
    leaf_id: str | None = None,
    agent: object | None = None,
    active_skills: list[ActiveSkill] | None = None,
    skill_dirs: list[str] | None = None,
) -> None:
    """Write the current session state to storage."""
    with active_session.save_lock:
        _save_active_session_locked(
            active_session,
            messages,
            conversation_entries=conversation_entries,
            leaf_id=leaf_id,
            agent=agent,
            active_skills=active_skills,
            skill_dirs=skill_dirs,
        )


def _save_active_session_locked(
    active_session: StoredSession,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None,
    leaf_id: str | None,
    agent: object | None,
    active_skills: list[ActiveSkill] | None,
    skill_dirs: list[str] | None,
) -> None:
    provider_state = (
        capture_provider_session_state(agent)
        if agent is not None
        else provider_session_state_from_values(
            provider_name=active_session.record.provider_name,
            model_id=active_session.record.model_id,
            reasoning_effort=active_session.record.reasoning_effort,
            context_window_tokens=active_session.record.context_window_tokens,
        )
    )
    active_session.record = active_session.store.save(
        active_session.id,
        messages,
        conversation_entries=conversation_entries,
        leaf_id=leaf_id,
        active_skills=(
            active_session.record.active_skills
            if active_skills is None
            else active_skills
        ),
        skill_dirs=(
            active_session.record.skill_dirs if skill_dirs is None else skill_dirs
        ),
        root=active_session.root,
        title=active_session.title,
        provider_name=provider_state.provider_name,
        model_id=provider_state.model_id,
        reasoning_effort=provider_state.reasoning_effort,
        context_window_tokens=provider_state.context_window_tokens,
        existing_record=active_session.record,
        tree_index=active_session.tree_index,
    )


def save_agent_session_state(
    active_session: StoredSession,
    state: AgentState,
    *,
    leaf_id: str | None = None,
    agent: object | None = None,
) -> None:
    """Write captured agent session state to storage."""
    save_active_session(
        active_session,
        state.messages if state.conversation_entries is None else [],
        conversation_entries=state.conversation_entries,
        leaf_id=leaf_id,
        agent=agent,
        active_skills=state.active_skills,
        skill_dirs=state.skill_dirs,
    )


def persist_session_state(
    active_session: StoredSession,
    agent: object,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None = None,
) -> None:
    """Sync skill state and persist the active session."""
    state = _state_matching_transcript(
        active_session,
        agent,
        messages,
        conversation_entries=conversation_entries,
    )
    save_agent_session_state(active_session, state, agent=agent)


def _state_matching_transcript(
    active_session: StoredSession,
    agent: object,
    messages: list[Message],
    *,
    conversation_entries: list[ConversationEntry] | None,
) -> AgentState:
    """Capture structured state without overriding a newer transcript."""
    if conversation_entries is not None:
        return _state_with_borrowed_conversation(agent, conversation_entries)
    if active_session.messages() == messages:
        return _state_with_borrowed_conversation(
            agent, active_session.active_entry_refs()
        )
    agent_state = capture_agent_state(agent)
    if agent_state.messages == messages:
        return agent_state
    session_branch = active_session.active_entries()
    matching_entries = (
        session_branch
        if transcript_messages_from_entries(session_branch) == messages
        else conversation_entries_from_messages(messages)
    )
    return capture_agent_state(
        agent,
        conversation_entries=matching_entries,
    )


def _state_with_borrowed_conversation(
    agent: object,
    conversation_entries: list[ConversationEntry],
) -> AgentState:
    """Capture agent metadata while borrowing a synchronous save snapshot."""
    state = capture_agent_state(agent, conversation_entries=[])
    state.conversation_entries = conversation_entries
    return state
