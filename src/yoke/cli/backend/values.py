"""Transport-independent presentation values used by terminal clients."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yoke.agent.models import ConversationEntry, Message
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.image_input import ImageAttachment

if TYPE_CHECKING:
    from yoke.cli.runtime.base import ActiveSession


type Checkpoint = Callable[[object, list[Message], list[ConversationEntry]], None]


@dataclass(slots=True)
class LoadedPromptQueue:
    """CLI queue values and the revision read with those values."""

    prompts: list[PendingPrompt]
    pending_images: list[ImageAttachment]
    revision: int


class PromptQueueRevisionConflict(RuntimeError):
    """A full queue replacement was based on an obsolete revision."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "Prompt queue changed "
            f"(expected revision {expected_revision}, found {actual_revision})"
        )


@dataclass(slots=True)
class CompactionSnapshot:
    """A daemon compaction has already persisted; only presentation needs updating."""

    messages: list[Message]
    payload: dict[str, object]


@dataclass(slots=True)
class TreeNavigationResult:
    """Result of moving the active session leaf."""

    messages: list[Message]
    active_session: ActiveSession
    editor_text: str | None = None
    summary_created: bool = False
