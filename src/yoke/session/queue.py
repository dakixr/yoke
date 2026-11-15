"""Shared persisted prompt-queue values and their database transactions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from yoke.agent.models import Message
from yoke.session.inputs import ensure_input_row, session_database, sync_input_row


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
    """Crash-safe prompt queue shared by CLI and HTTP processes."""

    version: int = 1
    revision: int = 0
    prompts: list[PersistedPendingInput] = Field(default_factory=list)
    pending_images: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class PromptQueueTransaction:
    """One queue snapshot read inside an open write transaction.

    ``commit`` writes the snapshot into that transaction; it becomes durable
    when the surrounding ``prompt_queue_transaction`` block exits normally.
    """

    snapshot: PersistedPromptQueue
    _connection: sqlite3.Connection
    _session_id: str

    def commit(self) -> None:
        """Write the snapshot while the queue transaction is still held."""
        _write_queue(self._connection, self._session_id, self.snapshot)


def load_prompt_queue_snapshot(
    session_directory: Path,
    session_id: str,
) -> PersistedPromptQueue:
    """Load one committed queue, empty when the session has none."""
    with session_database(session_directory).read() as connection:
        return _read_queue(connection, session_id)


def load_prompt_queue_snapshots(
    session_directory: Path,
    session_ids: Iterable[str],
) -> dict[str, PersistedPromptQueue]:
    """Load selected queues from one consistent snapshot."""
    requested = list(dict.fromkeys(session_ids))
    if not requested:
        return {}
    with session_database(session_directory).read() as connection:
        return {
            session_id: _read_queue(connection, session_id) for session_id in requested
        }


def write_prompt_queue_snapshot(
    session_directory: Path,
    session_id: str,
    snapshot: PersistedPromptQueue,
) -> None:
    """Persist one queue snapshot without resetting its revision."""
    with session_database(session_directory).write() as connection:
        _write_queue(connection, session_id, snapshot)


@contextmanager
def prompt_queue_transaction(
    session_directory: Path,
    session_id: str,
) -> Iterator[PromptQueueTransaction]:
    """Load one queue inside a write transaction for read-modify-write.

    The transaction excludes every other writer, in this and other processes,
    until the block exits. Admission, event and metadata writes made inside
    the block commit atomically with the queue; an exception rolls them back.
    """
    with session_database(session_directory).write() as connection:
        yield PromptQueueTransaction(
            snapshot=_read_queue(connection, session_id),
            _connection=connection,
            _session_id=session_id,
        )


def _read_queue(
    connection: sqlite3.Connection, session_id: str
) -> PersistedPromptQueue:
    meta = connection.execute(
        "SELECT queue_rev, pending_images FROM session_queues WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    prompts = [
        PersistedPendingInput.model_validate_json(pending)
        for (pending,) in connection.execute(
            "SELECT pending FROM inputs WHERE session_id = ? AND position IS NOT NULL "
            "ORDER BY position",
            (session_id,),
        )
    ]
    if meta is None:
        return PersistedPromptQueue(prompts=prompts)
    return PersistedPromptQueue(
        revision=int(meta[0]),
        prompts=prompts,
        pending_images=json.loads(meta[1]),
    )


def _write_queue(
    connection: sqlite3.Connection,
    session_id: str,
    snapshot: PersistedPromptQueue,
) -> None:
    ids = [item.id for item in snapshot.prompts]
    if len(set(ids)) != len(ids):
        raise ValueError("A prompt queue cannot contain the same input id twice.")
    previous = [
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM inputs WHERE session_id = ? AND position IS NOT NULL",
            (session_id,),
        )
    ]
    connection.execute(
        "UPDATE inputs SET position = NULL, pending = NULL "
        "WHERE session_id = ? AND position IS NOT NULL",
        (session_id,),
    )
    for position, item in enumerate(snapshot.prompts):
        ensure_input_row(connection, session_id, item.id)
        connection.execute(
            "UPDATE inputs SET position = ?, pending = ? WHERE session_id = ? AND id = ?",
            (position, item.model_dump_json(), session_id, item.id),
        )
    for input_id in dict.fromkeys([*previous, *ids]):
        sync_input_row(connection, session_id, input_id)
    if snapshot.revision == 0 and not snapshot.prompts and not snapshot.pending_images:
        connection.execute(
            "DELETE FROM session_queues WHERE session_id = ?", (session_id,)
        )
        return
    connection.execute(
        "INSERT INTO session_queues (session_id, queue_rev, pending_images) "
        "VALUES (?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET "
        "queue_rev = excluded.queue_rev, pending_images = excluded.pending_images",
        (session_id, snapshot.revision, json.dumps(snapshot.pending_images)),
    )


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
