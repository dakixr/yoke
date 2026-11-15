"""Shared persisted prompt-queue values and sidecar I/O."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from yoke._file_io import atomic_write_text, exclusive_file_lock
from yoke.agent.models import Message


class PersistedPendingInput(BaseModel):
    """One queued or steering input waiting for promotion."""

    id: str
    prompt: str
    attachments: list[dict[str, str]] = Field(default_factory=list)
    continuation: bool = False
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


class QueueCandidate(Protocol):
    """The scheduling fields shared by native and CLI queue items."""

    @property
    def kind(self) -> str: ...

    @property
    def paused(self) -> bool: ...


def next_eligible_index[Item: QueueCandidate](
    items: Sequence[Item],
    *,
    allow_queue: bool,
    runnable: Callable[[Item], bool] | None = None,
) -> int | None:
    """Prefer runnable steering inputs, retaining order within each delivery kind."""

    def eligible(item: Item) -> bool:
        return not item.paused and (runnable is None or runnable(item))

    for index, item in enumerate(items):
        if item.kind == "steering" and eligible(item):
            return index
    if allow_queue:
        for index, item in enumerate(items):
            if eligible(item):
                return index
    return None


def remove_occurrences[Item, Key](
    items: Sequence[Item], keys: Sequence[Key], *, key: Callable[[Item], Key]
) -> list[Item]:
    """Remove one matching occurrence per requested key, preserving duplicates."""
    remaining = list(items)
    for value in keys:
        index = next(
            (i for i, item in enumerate(remaining) if key(item) == value), None
        )
        if index is not None:
            remaining.pop(index)
    return remaining


def move_queue_item[Item](
    items: list[Item], index: int, target: int, *, after: bool = False
) -> None:
    """Move one item relative to a position in the original queue."""
    item = items.pop(index)
    if index < target:
        target -= 1
    items.insert(target + int(after), item)
