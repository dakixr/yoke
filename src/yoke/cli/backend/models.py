"""Prompt and turn values shared by the backend and terminal UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from yoke.agent.loop import AgentResult
from yoke.agent.models import ConversationEntry, Message


@dataclass(slots=True)
class TurnSuccess:
    """Successful turn result."""

    result: AgentResult


@dataclass(slots=True)
class TurnFailure:
    """Failed turn result."""

    error: Exception
    messages: list[Message] | None = None
    conversation_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class TurnStopped:
    """Stopped turn result."""

    result: AgentResult | None = None
    messages: list[Message] | None = None
    conversation_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class PendingPrompt:
    """Queued or steering prompt waiting behind the active turn."""

    prompt: str
    kind: Literal["queued", "steering"] = "queued"
    user_message: Message | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    paused: bool = False
    attachments: list[dict[str, str]] = field(default_factory=list)
    continuation: bool = False
    pause_cause: str | None = None

    def copy_for_queue(self) -> PendingPrompt:
        """Return a mutable copy preserving queue metadata."""
        return PendingPrompt(
            self.prompt,
            kind=self.kind,
            user_message=self.user_message,
            id=self.id,
            created_at=self.created_at,
            paused=self.paused,
            attachments=[dict(attachment) for attachment in self.attachments],
            continuation=self.continuation,
            pause_cause=self.pause_cause,
        )
