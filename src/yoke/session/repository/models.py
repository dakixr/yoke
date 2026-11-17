"""Session persistence models."""

from __future__ import annotations

from pydantic import BaseModel, Field, PrivateAttr

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.state import transcript_messages_from_entries

SESSION_INDEX_SUMMARY_VERSION = 3


class SessionTreeConflict(ValueError):
    """A tree edit was based on an obsolete stored tree revision."""

    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"Session {session_id} tree changed (expected revision {expected}, "
            f"found {actual})."
        )
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


class SessionRecord(BaseModel):
    """Persisted CLI session state."""

    version: int = 5
    id: str
    # SQLite provenance is not part of the frozen legacy record projection.
    origin: str = Field(default="cli", exclude=True)
    conversation_entries: list[ConversationEntry] = Field(default_factory=list)
    leaf_id: str | None = None
    active_skills: list[ActiveSkill] = Field(default_factory=list)
    skill_dirs: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    last_user_message_at: str | None = None
    root: str | None = None
    title: str | None = None
    pinned: bool = False
    archived_at: str | None = None
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None
    context_usage: dict[str, object] | None = None
    _history_loaded: bool = PrivateAttr(default=True)
    _tree_rev: int | None = PrivateAttr(default=None)

    @property
    def tree_revision(self) -> int | None:
        """Stored tree revision this record was read or written at, if known."""
        return self._tree_rev

    def stamp_tree_revision(self, revision: int | None) -> None:
        """Record the stored tree revision this value reflects."""
        self._tree_rev = revision

    @property
    def history_loaded(self) -> bool:
        """Whether conversation_entries holds the complete stored history."""
        return self._history_loaded

    def mark_summary(self) -> None:
        """Mark this record as metadata only, without conversation history."""
        self._history_loaded = False

    @property
    def messages(self) -> list[Message]:
        """Transcript messages in the session."""
        if not self._history_loaded:
            return []
        return transcript_messages_from_entries(
            self.conversation_entries,
            leaf_id=self.leaf_id,
        )


class SessionIndexEntry(BaseModel):
    """Searchable session summary stored in the CLI session index."""

    id: str
    # SQLite provenance is not part of the frozen legacy record projection.
    origin: str = Field(default="cli", exclude=True)
    root: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_user_message_at: str | None = None
    pinned: bool = False
    archived_at: str | None = None
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None
    context_usage: dict[str, object] | None = None
    leaf_id: str | None = None
    active_skills: list[ActiveSkill] = Field(default_factory=list)
    skill_dirs: list[str] = Field(default_factory=list)
    entry_count: int | None = None
    file_size: int | None = None
    file_mtime_ns: int | None = None
    search_snippet: str | None = Field(default=None, exclude=True)
    summary_version: int = 0

    def to_record(self) -> SessionRecord:
        """Convert the index entry into a partial session record."""
        record = SessionRecord(
            id=self.id,
            origin=self.origin,
            root=self.root,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            last_user_message_at=self.last_user_message_at,
            pinned=self.pinned,
            archived_at=self.archived_at,
            provider_name=self.provider_name,
            model_id=self.model_id,
            reasoning_effort=self.reasoning_effort,
            context_window_tokens=self.context_window_tokens,
            context_usage=(dict(self.context_usage) if self.context_usage else None),
            leaf_id=self.leaf_id,
            active_skills=[skill.model_copy(deep=True) for skill in self.active_skills],
            skill_dirs=list(self.skill_dirs),
        )
        record.mark_summary()
        return record


class SessionIndex(BaseModel):
    """Persistent index of saved CLI sessions."""

    session_schema_version: int | None = None
    storage_schema_version: int | None = None
    sessions: dict[str, SessionIndexEntry] = Field(default_factory=dict)
