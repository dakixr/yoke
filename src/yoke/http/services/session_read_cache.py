"""Bounded read cache for expensive persisted HTTP session snapshots."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.cli.session.io import SESSION_ENTRY_EVENT
from yoke.cli.session.io import SESSION_ENTRY_METADATA_EVENT
from yoke.cli.session.io import SESSION_METADATA_EVENT
from yoke.cli.session.models import SessionRecord
from yoke.session import SessionStore


@dataclass(frozen=True, slots=True)
class SessionReadSnapshot:
    """One file-signature-consistent session read projection."""

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
    singleflight, every request can parse the same large JSONL file at once.
    The cache is bounded by source-file bytes and entry count, while always
    retaining the newest snapshot even if one unusually large session exceeds
    the nominal byte budget.
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
            if prior is not None and signature[0] > prior.signature[0]:
                appended = self._append_snapshot(session_id, prior, signature)
                if appended is not None:
                    self._store(session_id, appended)
                    return appended
            record = self.store.load(session_id)
            final_signature = self.store.session_file_signature(session_id) or signature
            entries_by_id = {entry.id: entry for entry in record.conversation_entries}
            active_path_entries = tuple(
                _active_path_entries(entries_by_id, record.leaf_id)
            )
            active_entries = tuple(
                entry
                for entry in active_path_entries
                if entry.kind not in {"instruction", "memory_snapshot"}
            )
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
        signature: tuple[int, int],
    ) -> SessionReadSnapshot | None:
        path = self.store.directory / f"{session_id}.jsonl"
        byte_count = signature[0] - prior.signature[0]
        try:
            with path.open("rb") as handle:
                handle.seek(prior.signature[0])
                raw = handle.read(byte_count)
        except OSError:
            return None
        if not raw or not raw.endswith(b"\n"):
            return None

        # ``SessionStore.save`` intentionally extends an append-proven existing
        # record in place to avoid copying a huge historical prefix. A cached
        # record can therefore already expose the new Python objects while its
        # file signature and lookup maps still describe the old snapshot. Trim
        # back to the indexed prefix here, then replay the durable JSONL delta
        # exactly once.
        known_entry_count = len(prior.entry_positions)
        entries = list(prior.record.conversation_entries[:known_entry_count])
        by_id = dict(prior.entries_by_id)
        positions = dict(prior.entry_positions)
        changes: dict[str, object] = {}
        appended_entries: list[ConversationEntry] = []
        replaced_topology = False
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                payload = from_json(line)
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            payload_type = payload.get("type")
            if payload_type == SESSION_METADATA_EVENT:
                changes.update(
                    {key: value for key, value in payload.items() if key != "type"}
                )
                continue
            if payload_type == SESSION_ENTRY_EVENT:
                raw_entry = payload.get("entry")
                if not isinstance(raw_entry, dict):
                    return None
                entry = ConversationEntry.model_validate(raw_entry)
                if entry.id in positions:
                    # Duplicate entry events are valid replacement semantics but
                    # rare. A full reload keeps that edge case simple and exact.
                    return None
                positions[entry.id] = len(entries)
                entries.append(entry)
                by_id[entry.id] = entry
                appended_entries.append(entry)
                continue
            if payload_type == SESSION_ENTRY_METADATA_EVENT:
                entry_id = payload.get("entry_id")
                metadata = payload.get("metadata")
                if not isinstance(entry_id, str) or not isinstance(metadata, dict):
                    continue
                position = positions.get(entry_id)
                existing = by_id.get(entry_id)
                if position is None or existing is None:
                    continue
                replacement = existing.model_copy(
                    update={"metadata": metadata},
                    deep=True,
                )
                entries[position] = replacement
                by_id[entry_id] = replacement
                replaced_topology = True
                continue
            return None

        record = prior.record.model_copy(
            update={
                **changes,
                "conversation_entries": entries,
            }
        )
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
                *(
                    entry
                    for entry in appended_entries
                    if entry.kind not in {"instruction", "memory_snapshot"}
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
            active_entries = tuple(
                entry
                for entry in active_path_entries
                if entry.kind not in {"instruction", "memory_snapshot"}
            )
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
