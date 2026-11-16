"""Read-only session presentation values for the daemon-backed CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.cli.backend.daemon import DaemonSessionBackend

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import ActiveSkill
from yoke.http.models.session import SessionInfo, SessionResponse


@dataclass(slots=True)
class DaemonSessionRecord:
    """Metadata only. Conversation execution and history stay in the daemon."""

    title: str | None = None
    pinned: bool = False
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    entry_count: int = 0
    conversation_entries: list[ConversationEntry] = field(default_factory=list)
    active_skills: list[ActiveSkill] = field(default_factory=list)
    leaf_id: str | None = None


class DaemonActiveSession:
    """Session metadata refreshed explicitly before commands read it."""

    def __init__(self, backend: DaemonSessionBackend, data: SessionInfo) -> None:
        self.backend = backend
        self.connection = backend.connection
        self.id = data.id
        self.save_lock = RLock()
        self.update(data)

    def update(self, data: SessionInfo) -> None:
        self.root = Path(data.location.directory)
        self.title = data.title
        self.database_path = Path(data.database_path) if data.database_path else None
        self.record = DaemonSessionRecord(
            title=data.title,
            pinned=data.pinned,
            provider_name=data.selection.provider,
            model_id=data.selection.model,
            reasoning_effort=data.selection.reasoning_effort,
            context_window_tokens=data.context_window_tokens,
            created_at=data.time.created,
            updated_at=data.time.updated,
            entry_count=data.tree.entry_count,
            leaf_id=data.tree.leaf_id,
        )

    def refresh(self) -> None:
        self.update(
            SessionResponse.model_validate(self.connection.session_request("GET")).data
        )

    def active_entry_refs(self) -> list[ConversationEntry]:
        return []

    def active_entries(self) -> list[ConversationEntry]:
        return []

    def messages(self) -> list[Message]:
        from yoke.cli.backend.daemon.turn import transcript

        return transcript(self.connection)
