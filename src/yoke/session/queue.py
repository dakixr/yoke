"""Shared persisted prompt-queue values and sidecar I/O."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from yoke.agent.models import Message


class PersistedPendingInput(BaseModel):
    """One queued or steering input waiting for promotion."""

    id: str
    prompt: str
    attachments: list[dict[str, str]] = Field(default_factory=list)
    kind: Literal["queued", "steering"] = "queued"
    created_at: str
    paused: bool = False
    user_message: Message | None = None


class PersistedPromptQueue(BaseModel):
    """Crash-safe prompt queue sidecar shared by CLI and HTTP readers."""

    version: int = 1
    revision: int = 0
    prompts: list[PersistedPendingInput] = Field(default_factory=list)
    pending_images: list[str] = Field(default_factory=list)


def prompt_queue_path(session_directory: Path, session_id: str) -> Path:
    """Return the sidecar path for one persisted session queue."""
    return session_directory / "queues" / f"{session_id}.json"


def load_prompt_queue_snapshot(
    session_directory: Path,
    session_id: str,
) -> PersistedPromptQueue:
    """Load one queue sidecar, returning an empty snapshot when absent/corrupt."""
    path = prompt_queue_path(session_directory, session_id)
    if not path.exists():
        return PersistedPromptQueue()
    try:
        return PersistedPromptQueue.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValidationError, ValueError):
        return PersistedPromptQueue()


def write_prompt_queue_snapshot(
    session_directory: Path,
    session_id: str,
    snapshot: PersistedPromptQueue,
) -> None:
    """Atomically write or remove one queue sidecar snapshot."""
    path = prompt_queue_path(session_directory, session_id)
    if not snapshot.prompts and not snapshot.pending_images:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(path)
