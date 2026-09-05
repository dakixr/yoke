"""Shared persisted prompt-queue values and sidecar I/O."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from yoke.agent.models import Message
from yoke._file_io import atomic_write_text
from yoke._file_io import exclusive_file_lock


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


@dataclass(slots=True)
class PromptQueueTransaction:
    """One queue snapshot held under its cross-process mutation lock."""

    snapshot: PersistedPromptQueue
    _path: Path

    def commit(self) -> None:
        """Persist the transaction snapshot while retaining the queue lock."""
        _write_prompt_queue_path(self._path, self.snapshot)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
        _write_prompt_queue_path(path, snapshot)


@contextmanager
def prompt_queue_transaction(
    session_directory: Path,
    session_id: str,
) -> Iterator[PromptQueueTransaction]:
    """Lock, load, and expose one queue for a read-modify-write transaction."""
    path = prompt_queue_path(session_directory, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
        yield PromptQueueTransaction(
            snapshot=_load_prompt_queue_path(path),
            _path=path,
        )


def _write_prompt_queue_path(path: Path, snapshot: PersistedPromptQueue) -> None:
    """Persist a queue snapshot while the caller holds its mutation lock."""
    if snapshot.revision == 0 and not snapshot.prompts and not snapshot.pending_images:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    atomic_write_text(path, snapshot.model_dump_json(indent=2))
