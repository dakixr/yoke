"""Stateless snapshot adapter for runtime callers of the former read cache."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.http.services.projectors import public_chat_entries
from yoke.session import SessionStore
from yoke.session.repository.models import SessionRecord


@dataclass(frozen=True, slots=True)
class SessionReadSnapshot:
    """One signature-consistent session read projection.

    ``signature`` is the store's (stored message bytes, row revision).
    """

    signature: tuple[int, int]
    record: SessionRecord
    active_path_entries: tuple[ConversationEntry, ...]
    active_entries: tuple[ConversationEntry, ...]
    entries_by_id: dict[str, ConversationEntry]
    entry_positions: dict[str, int]

    def owned_active_path(self) -> list[ConversationEntry]:
        """Return deep-copied active entries safe for arbitrary mutation."""
        return [entry.model_copy(deep=True) for entry in self.active_path_entries]

    def runtime_active_path(self) -> list[ConversationEntry]:
        """Own entry shells while borrowing immutable historical message values."""
        return [
            entry.model_copy(update={"metadata": dict(entry.metadata)}, deep=False)
            for entry in self.active_path_entries
        ]


class SessionSnapshots:
    """Compatibility adapter; every call reads a fresh database snapshot.

    Runtime integrations still pass this object. HTTP pagination uses SQL and
    never calls it. No parsed records or per-session locks are retained.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def is_current(self, session_id: str) -> bool:
        """There is no retained snapshot."""
        return False

    def get(self, session_id: str) -> SessionReadSnapshot:
        with self.store.database.read():
            signature = self.store.session_file_signature(session_id)
            if signature is None:
                raise FileNotFoundError(session_id)
            record = self.store.load(session_id)
            by_id = {entry.id: entry for entry in record.conversation_entries}
            active = tuple(_active_path_entries(by_id, record.leaf_id))
            return SessionReadSnapshot(
                signature,
                record,
                active,
                tuple(public_chat_entries(active)),
                by_id,
                {
                    entry.id: index
                    for index, entry in enumerate(record.conversation_entries)
                },
            )


def _active_path_entries(
    entries_by_id: dict[str, ConversationEntry],
    leaf_id: str | None,
) -> list[ConversationEntry]:
    """Return one validated root-to-leaf path from an existing id lookup."""
    if leaf_id is None:
        return []
    reverse_path: list[ConversationEntry] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("Session tree contains a parent cycle.")
        seen.add(current)
        entry = entries_by_id.get(current)
        if entry is None:
            break
        reverse_path.append(entry)
        current = entry.parent_id
    reverse_path.reverse()
    return reverse_path
