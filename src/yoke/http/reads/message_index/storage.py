"""Topology snapshots read from the session database, with background warming."""

from __future__ import annotations

import time

from yoke.agent.models import ConversationEntry
from yoke.http.reads.message_index.cache import SnapshotCache
from yoke.http.reads.message_index.legacy import (
    mark_legacy_tool_context_locations,
)
from yoke.http.reads.message_index.models import MessageIndexSnapshot
from yoke.http.reads.message_index.models import kind as location_kind
from yoke.session import SessionStore


class IndexStorage:
    """Own cached entry topology keyed by the session's stored revision."""

    def __init__(self, store: SessionStore, max_cached_sessions: int) -> None:
        self.store = store
        self.cache = SnapshotCache(max_cached_sessions)

    def warm_async(self, session_id: str) -> None:
        """Build a topology snapshot after latency-sensitive reads return."""
        if self.cache.start_warming(session_id):
            self.cache.submit(lambda: self.warm_one(session_id))

    def close(self) -> None:
        """Stop accepting background index work during application shutdown."""
        self.cache.close()

    def warm_one(self, session_id: str) -> None:
        try:
            # Give the HTTP response and browser paint a head start.
            time.sleep(0.2)
            self.ensure(session_id)
        finally:
            self.cache.finish_warming(session_id)

    def current_snapshot(self, session_id: str) -> MessageIndexSnapshot | None:
        """Return the topology for the current revision, building it if needed."""
        return self.ensure(session_id)

    def ensure(self, session_id: str) -> MessageIndexSnapshot | None:
        signature = self.store.session_file_signature(session_id)
        if signature is None:
            return None
        cached = self.cache.get(session_id)
        if cached is not None and _matches(cached, signature):
            return cached
        with self.cache.session_lock(session_id):
            cached = self.cache.get(session_id)
            signature = self.store.session_file_signature(session_id)
            if signature is None:
                return None
            if cached is not None and _matches(cached, signature):
                return cached
            topology = self.store.entry_topology(session_id)
            if topology is None:
                return None
            entries: dict[str, list[object]] = {
                entry_id: [parent_id, kind, position]
                for position, (entry_id, parent_id, kind) in enumerate(topology.rows)
            }

            def read(entry_id: str) -> ConversationEntry | None:
                found = self.store.read_entries(session_id, [entry_id])
                return found[0] if found else None

            mark_legacy_tool_context_locations(read, entries)
            snapshot = MessageIndexSnapshot(
                signature=topology.signature,
                leaf_id=topology.leaf_id,
                entries=entries,
            )
            self.cache.put(session_id, snapshot)
            return snapshot

    def read_entries(
        self,
        session_id: str,
        snapshot: MessageIndexSnapshot,
        entry_ids: list[str],
    ) -> list[ConversationEntry] | None:
        if any(entry_id not in snapshot.entries for entry_id in entry_ids):
            return None
        entries = self.store.read_entries(session_id, entry_ids)
        if entries is None:
            return None
        for entry in entries:
            if (
                location_kind(snapshot.entries[entry.id]) == "tool_context"
                and entry.kind != "tool_context"
            ):
                entry.kind = "tool_context"
        return entries


def _matches(snapshot: MessageIndexSnapshot, signature: tuple[int, int]) -> bool:
    return snapshot.signature == signature
