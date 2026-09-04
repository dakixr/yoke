"""Shared persisted prompt-queue values and sidecar I/O."""

from __future__ import annotations

from collections.abc import Iterable
import os
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
    return _load_prompt_queue_path(prompt_queue_path(session_directory, session_id))


def load_prompt_queue_snapshots(
    session_directory: Path,
    session_ids: Iterable[str],
) -> dict[str, PersistedPromptQueue]:
    """Load selected queue sidecars with one directory enumeration."""
    requested = set(session_ids)
    if not requested:
        return {}
    requested_keys = {os.path.normcase(session_id) for session_id in requested}
    normalized_suffix = os.path.normcase(".json")
    queue_directory = session_directory / "queues"
    try:
        paths = queue_directory.iterdir()
        matching = {
            os.path.normcase(path.stem): path
            for path in paths
            if os.path.normcase(path.suffix) == normalized_suffix
            and os.path.normcase(path.stem) in requested_keys
        }
    except (FileNotFoundError, NotADirectoryError):
        matching = {}
    except OSError:
        return {
            session_id: load_prompt_queue_snapshot(session_directory, session_id)
            for session_id in requested
        }
    return {
        session_id: (
            _load_prompt_queue_path(path)
            if (path := matching.get(os.path.normcase(session_id))) is not None
            else PersistedPromptQueue()
        )
        for session_id in requested
    }


def _load_prompt_queue_path(path: Path) -> PersistedPromptQueue:
    """Load one known queue path, returning an empty snapshot on failure."""
    try:
        return PersistedPromptQueue.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValidationError, ValueError):
        return PersistedPromptQueue()


def write_prompt_queue_snapshot(
    session_directory: Path,
    session_id: str,
    snapshot: PersistedPromptQueue,
) -> None:
    """Atomically persist one queue snapshot without resetting its revision."""
    path = prompt_queue_path(session_directory, session_id)
    if snapshot.revision == 0 and not snapshot.prompts and not snapshot.pending_images:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(path)
