"""Session persistence models."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.state import transcript_messages_from_entries


class SessionRecord(BaseModel):
    """Persisted CLI session state."""

    version: int = 5
    id: str
    conversation_entries: list[ConversationEntry] = Field(default_factory=list)
    leaf_id: str | None = None
    active_skills: list[ActiveSkill] = Field(default_factory=list)
    skill_dirs: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    root: str | None = None
    title: str | None = None
    pinned: bool = False
    archived_at: str | None = None
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None

    @property
    def messages(self) -> list[Message]:
        """Transcript messages in the session."""
        return transcript_messages_from_entries(
            self.conversation_entries,
            leaf_id=self.leaf_id,
        )


class SessionIndexEntry(BaseModel):
    """Searchable session summary stored in the CLI session index."""

    id: str
    root: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    pinned: bool = False
    archived_at: str | None = None

    def to_record(self) -> SessionRecord:
        """Convert the index entry into a partial session record."""
        return SessionRecord(
            id=self.id,
            root=self.root,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            pinned=self.pinned,
            archived_at=self.archived_at,
        )


class SessionIndex(BaseModel):
    """Persistent index of saved CLI sessions."""

    session_schema_version: int | None = None
    storage_schema_version: int | None = None
    sessions: dict[str, SessionIndexEntry] = Field(default_factory=dict)
