"""Bounded read cache for expensive persisted HTTP session snapshots."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

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


class SessionReadCache:
    """Singleflight and retain a few parsed sessions for HTTP reads.

    A browser opens one session by issuing several concurrent requests. Without
    singleflight, every request can decode the same large session at once.
    The cache is bounded by stored message bytes and entry count, while always
    retaining the newest snapshot even if one unusually large session exceeds
    the nominal byte budget. A newer revision is caught up from the rows
    written since the cached one, unless the entries were rewritten.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        max_entries: int = 4,
        max_source_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.store = store
        self.max_entries = max_entries
        self.max_source_bytes = max_source_bytes
        self._lock = Lock()
        self._session_locks: dict[str, Lock] = {}
        self._entries: OrderedDict[str, SessionReadSnapshot] = OrderedDict()
        self._source_bytes = 0

    def is_current(self, session_id: str) -> bool:
        """Return whether a signature-current parsed snapshot is already cached."""
        signature = self.store.session_file_signature(session_id)
        if signature is None:
            return False
        return self._get_cached(session_id, signature) is not None

    def get(self, session_id: str) -> SessionReadSnapshot:
        signature = self.store.session_file_signature(session_id)
        if signature is None:
            raise FileNotFoundError(session_id)
        cached = self._get_cached(session_id, signature)
        if cached is not None:
            return cached

        session_lock = self._session_lock(session_id)
        with session_lock:
            signature = self.store.session_file_signature(session_id)
            if signature is None:
                raise FileNotFoundError(session_id)
            cached = self._get_cached(session_id, signature)
            if cached is not None:
                return cached
            prior = self._get_any_cached(session_id)
            if prior is not None and signature[1] > prior.signature[1]:
                appended = self._append_snapshot(session_id, prior)
                if appended is not None:
                    self._store(session_id, appended)
                    return appended
            # Label the snapshot with the signature read before loading. A write
            # in between is then caught up by the next delta, never hidden.
            record = self.store.load(session_id)
            final_signature = signature
            entries_by_id = {entry.id: entry for entry in record.conversation_entries}
            active_path_entries = tuple(
                _active_path_entries(entries_by_id, record.leaf_id)
            )
            active_entries = tuple(public_chat_entries(active_path_entries))
            snapshot = SessionReadSnapshot(
                signature=final_signature,
                record=record,
                active_path_entries=active_path_entries,
                active_entries=active_entries,
                entries_by_id=entries_by_id,
                entry_positions={
                    entry.id: index
                    for index, entry in enumerate(record.conversation_entries)
                },
            )
            self._store(session_id, snapshot)
            return snapshot

    def _get_any_cached(self, session_id: str) -> SessionReadSnapshot | None:
        with self._lock:
            snapshot = self._entries.get(session_id)
            if snapshot is not None:
                self._entries.move_to_end(session_id)
            return snapshot

    def _append_snapshot(
        self,
        session_id: str,
        prior: SessionReadSnapshot,
    ) -> SessionReadSnapshot | None:
        delta = self.store.read_delta(session_id, prior.signature[1])
        if delta is None:
            return None
        signature = delta.signature

        # ``SessionStore.save`` intentionally extends an append-proven existing
        # record in place to avoid copying a huge historical prefix. A cached
        # record can therefore already expose the new Python objects while its
        # signature and lookup maps still describe the old snapshot. Trim back
        # to the indexed prefix here, then apply the stored delta exactly once.
        known_entry_count = len(prior.entry_positions)
        entries = list(prior.record.conversation_entries[:known_entry_count])
        by_id = dict(prior.entries_by_id)
        positions = dict(prior.entry_positions)
        appended_entries: list[ConversationEntry] = []
        replaced_topology = False
        for entry in delta.entries:
            position = positions.get(entry.id)
            if position is None:
                positions[entry.id] = len(entries)
                entries.append(entry)
                by_id[entry.id] = entry
                appended_entries.append(entry)
                continue
            if appended_entries or by_id[entry.id].parent_id != entry.parent_id:
                # Stored messages are immutable, so an edited row only carries
                # new metadata. Anything else is rare; reload it exactly.
                return None
            entries[position] = entry
            by_id[entry.id] = entry
            replaced_topology = True

        record = delta.record.model_copy(update={"conversation_entries": entries})
        active_path_entries: tuple[ConversationEntry, ...]
        active_entries: tuple[ConversationEntry, ...]
        if (
            appended_entries
            and not replaced_topology
            and _extends_active_leaf(
                prior.record.leaf_id,
                record.leaf_id,
                appended_entries,
            )
        ):
            active_path_entries = (*prior.active_path_entries, *appended_entries)
            active_entries = (
                *prior.active_entries,
                *_public_chat_appended_entries(
                    prior.active_path_entries,
                    appended_entries,
                ),
            )
        elif (
            not appended_entries
            and not replaced_topology
            and record.leaf_id == prior.record.leaf_id
        ):
            active_path_entries = prior.active_path_entries
            active_entries = prior.active_entries
        else:
            active_path_entries = tuple(_active_path_entries(by_id, record.leaf_id))
            active_entries = tuple(public_chat_entries(active_path_entries))
        return SessionReadSnapshot(
            signature=signature,
            record=record,
            active_path_entries=active_path_entries,
            active_entries=active_entries,
            entries_by_id=by_id,
            entry_positions=positions,
        )

    def _get_cached(
        self,
        session_id: str,
        signature: tuple[int, int],
    ) -> SessionReadSnapshot | None:
        with self._lock:
            snapshot = self._entries.get(session_id)
            if snapshot is None:
                return None
            if snapshot.signature != signature:
                return None
            self._entries.move_to_end(session_id)
            return snapshot

    def _session_lock(self, session_id: str) -> Lock:
        with self._lock:
            return self._session_locks.setdefault(session_id, Lock())

    def _store(self, session_id: str, snapshot: SessionReadSnapshot) -> None:
        with self._lock:
            self._remove_locked(session_id)
            self._entries[session_id] = snapshot
            self._source_bytes += snapshot.signature[0]
            while len(self._entries) > 1 and (
                len(self._entries) > self.max_entries
                or self._source_bytes > self.max_source_bytes
            ):
                oldest = next(iter(self._entries))
                self._remove_locked(oldest)

    def _remove_locked(self, session_id: str) -> None:
        snapshot = self._entries.pop(session_id, None)
        if snapshot is not None:
            self._source_bytes -= snapshot.signature[0]


def _extends_active_leaf(
    old_leaf_id: str | None,
    new_leaf_id: str | None,
    appended_entries: list[ConversationEntry],
) -> bool:
    parent = old_leaf_id
    for entry in appended_entries:
        if entry.parent_id != parent:
            return False
        parent = entry.id
    return new_leaf_id == parent


def _public_chat_appended_entries(
    prior_entries: tuple[ConversationEntry, ...],
    appended_entries: list[ConversationEntry],
) -> list[ConversationEntry]:
    """Filter an append using only the current turn's provenance ancestors."""
    start = next(
        (
            index
            for index in range(len(prior_entries) - 1, -1, -1)
            if prior_entries[index].kind == "user"
        ),
        0,
    )
    appended_ids = {entry.id for entry in appended_entries}
    scope = [*prior_entries[start:], *appended_entries]
    return [entry for entry in public_chat_entries(scope) if entry.id in appended_ids]


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
