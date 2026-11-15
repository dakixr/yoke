"""Snapshot caching, journal scanning, and background index warming."""

from __future__ import annotations

import time
from pathlib import Path

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.http.reads.message_index.cache import SnapshotCache
from yoke.http.reads.message_index.legacy import (
    mark_legacy_tool_context_locations,
)
from yoke.http.reads.message_index.models import (
    MessageIndexSnapshot,
    can_append,
    metadata_length,
    metadata_offset,
    snapshot_matches,
)
from yoke.http.reads.message_index.models import (
    entry_topology as parse_entry_topology,
)
from yoke.http.reads.message_index.models import kind as location_kind
from yoke.http.reads.message_index.models import length as location_length
from yoke.http.reads.message_index.models import offset as location_offset
from yoke.http.reads.message_index.models import (
    parent_id as location_parent_id,
)
from yoke.http.reads.message_index.models import (
    prefix_hash as calculate_prefix_hash,
)
from yoke.http.reads.message_index.sidecar import (
    link_sidecar,
    load_sidecar,
    write_sidecar,
)
from yoke.session import SessionStore
from yoke.session.repository.io import (
    SESSION_ENTRY_EVENT,
    SESSION_ENTRY_METADATA_EVENT,
    SESSION_JSONL_HEADER_TYPE,
    SESSION_JSONL_HEADER_VERSION,
    SESSION_METADATA_EVENT,
)


class IndexStorage:
    """Own cached topology, durable sidecars, and background warming."""

    def __init__(self, store: SessionStore, max_cached_sessions: int) -> None:
        self.store = store
        self.cache = SnapshotCache(max_cached_sessions)

    def clone_sidecar(self, source_session_id: str, target_session_id: str) -> None:
        """Seed a fork with the source topology snapshot without rescanning it."""
        # Sidecars are replaced atomically rather than mutated in place. A hard
        # link therefore gives the fork an immutable snapshot of the source index
        # without serializing or copying a potentially multi-megabyte topology map.
        # The fork's appended metadata is caught up incrementally on first use.
        if link_sidecar(self.store.directory, source_session_id, target_session_id):
            return
        snapshot = self.current_snapshot(source_session_id)
        if snapshot is None:
            return
        target = self.store.directory / f"{target_session_id}.jsonl"
        try:
            stat = target.stat()
        except OSError:
            return
        source_prefix_hash = calculate_prefix_hash(target, stat.st_size)
        if (
            source_prefix_hash != snapshot.prefix_hash
            or stat.st_size < snapshot.indexed_size
        ):
            return
        write_sidecar(self.store.directory, target_session_id, snapshot)

    def warm_async(self, session_id: str) -> None:
        """Build a durable topology sidecar after latency-sensitive reads return."""
        if self.cache.start_warming(session_id):
            self.cache.submit(lambda: self.warm_one(session_id))

    def close(self) -> None:
        """Stop accepting background index work during application shutdown."""
        self.cache.close()

    def warm_one(self, session_id: str) -> None:
        try:
            # Give the HTTP response and browser paint a head start. The full
            # source scan is deliberately background work.
            time.sleep(0.2)
            self.ensure(session_id)
        finally:
            self.cache.finish_warming(session_id)

    def current_snapshot(self, session_id: str) -> MessageIndexSnapshot | None:
        source = self.store.directory / f"{session_id}.jsonl"
        try:
            stat = source.stat()
        except OSError:
            return None
        source_prefix_hash = calculate_prefix_hash(source, stat.st_size)
        if source_prefix_hash is None:
            return None
        cached_snapshot = self.cache.get(session_id)
        if cached_snapshot is not None and snapshot_matches(
            cached_snapshot,
            stat.st_size,
            stat.st_mtime_ns,
            source_prefix_hash,
        ):
            return cached_snapshot
        sidecar_snapshot = load_sidecar(self.store.directory, session_id)
        if sidecar_snapshot is not None and snapshot_matches(
            sidecar_snapshot,
            stat.st_size,
            stat.st_mtime_ns,
            source_prefix_hash,
        ):
            self.cache.put(session_id, sidecar_snapshot)
            return sidecar_snapshot
        return None

    def ensure(self, session_id: str) -> MessageIndexSnapshot | None:
        source = self.store.directory / f"{session_id}.jsonl"
        try:
            stat = source.stat()
        except OSError:
            return None
        source_prefix_hash = calculate_prefix_hash(source, stat.st_size)
        if source_prefix_hash is None:
            return None
        cached_snapshot = self.cache.get(session_id)
        if cached_snapshot is not None and snapshot_matches(
            cached_snapshot,
            stat.st_size,
            stat.st_mtime_ns,
            source_prefix_hash,
        ):
            return cached_snapshot

        lock = self.cache.session_lock(session_id)
        with lock:
            try:
                stat = source.stat()
            except OSError:
                return None
            source_prefix_hash = calculate_prefix_hash(source, stat.st_size)
            if source_prefix_hash is None:
                return None
            cached_snapshot = self.cache.get(session_id)
            if cached_snapshot is not None and snapshot_matches(
                cached_snapshot,
                stat.st_size,
                stat.st_mtime_ns,
                source_prefix_hash,
            ):
                return cached_snapshot
            prior = cached_snapshot or load_sidecar(self.store.directory, session_id)
            if prior is not None and snapshot_matches(
                prior,
                stat.st_size,
                stat.st_mtime_ns,
                source_prefix_hash,
            ):
                self.cache.put(session_id, prior)
                return prior
            append_only = prior is not None and can_append(
                prior,
                stat.st_size,
                source_prefix_hash,
            )
            if append_only and prior is not None:
                snapshot = scan(
                    source,
                    start=prior.indexed_size,
                    source_size=stat.st_size,
                    source_mtime_ns=stat.st_mtime_ns,
                    prefix_hash=source_prefix_hash,
                    prior=prior,
                )
            else:
                snapshot = scan(
                    source,
                    start=0,
                    source_size=stat.st_size,
                    source_mtime_ns=stat.st_mtime_ns,
                    prefix_hash=source_prefix_hash,
                    prior=None,
                )
            if snapshot is None:
                return None
            self.cache.put(session_id, snapshot)
            # Rewriting a multi-megabyte topology cache for every appended
            # assistant/tool event would put O(session size) work back into
            # live refreshes. A stale sidecar is safe: the next daemon catches
            # it up from indexed_size. Persist complete rebuilds only.
            if not append_only:
                write_sidecar(self.store.directory, session_id, snapshot)
            return snapshot

    def read_entries(
        self,
        session_id: str,
        snapshot: MessageIndexSnapshot,
        entry_ids: list[str],
    ) -> list[ConversationEntry] | None:
        source = self.store.directory / f"{session_id}.jsonl"
        result: list[ConversationEntry] = []
        try:
            with source.open("rb") as handle:
                for entry_id in entry_ids:
                    location = snapshot.entries.get(entry_id)
                    if location is None:
                        return None
                    handle.seek(location_offset(location))
                    payload = from_json(handle.read(location_length(location)).strip())
                    if not isinstance(payload, dict):
                        return None
                    raw_entry = payload.get("entry")
                    if not isinstance(raw_entry, dict):
                        return None
                    entry = ConversationEntry.model_validate(raw_entry)
                    indexed_kind = location_kind(location)
                    if indexed_kind == "tool_context" and entry.kind != "tool_context":
                        entry.kind = "tool_context"
                    entry_metadata_offset = metadata_offset(location)
                    entry_metadata_length = metadata_length(location)
                    if (
                        entry_metadata_offset is not None
                        and entry_metadata_length is not None
                    ):
                        handle.seek(entry_metadata_offset)
                        metadata_event = from_json(
                            handle.read(entry_metadata_length).strip()
                        )
                        if isinstance(metadata_event, dict):
                            metadata = metadata_event.get("metadata")
                            if isinstance(metadata, dict):
                                entry = entry.model_copy(
                                    update={"metadata": metadata},
                                    deep=True,
                                )
                    result.append(entry)
        except (OSError, ValueError):
            return None
        return result


def scan(
    source: Path,
    *,
    start: int,
    source_size: int,
    source_mtime_ns: int,
    prefix_hash: str,
    prior: MessageIndexSnapshot | None,
) -> MessageIndexSnapshot | None:
    entries = dict(prior.entries) if prior is not None else {}
    leaf_id = prior.leaf_id if prior is not None else None
    saw_header = prior is not None
    indexed_size = start
    scanned_entry_ids: list[str] = []
    try:
        with source.open("rb") as handle:
            handle.seek(start)
            while handle.tell() < source_size:
                offset = handle.tell()
                line = handle.readline(source_size - offset)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                indexed_size = handle.tell()
                stripped = line[:-1]
                if stripped.endswith(b"\r"):
                    stripped = stripped[:-1]
                if not stripped:
                    continue
                topology = parse_entry_topology(stripped)
                if topology is not None:
                    entry_id, parent_id, kind = topology
                    existing = entries.get(entry_id)
                    entries[entry_id] = [
                        parent_id,
                        kind,
                        offset,
                        len(line),
                        metadata_offset(existing),
                        metadata_length(existing),
                    ]
                    scanned_entry_ids.append(entry_id)
                    continue
                try:
                    payload = from_json(stripped)
                except ValueError:
                    return None
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                if payload_type == SESSION_JSONL_HEADER_TYPE:
                    if payload.get("version") != SESSION_JSONL_HEADER_VERSION:
                        return None
                    saw_header = True
                elif payload_type == SESSION_METADATA_EVENT:
                    if "leaf_id" in payload:
                        value = payload.get("leaf_id")
                        if value is None or isinstance(value, str):
                            leaf_id = value
                elif payload_type == SESSION_ENTRY_METADATA_EVENT:
                    entry_id = payload.get("entry_id")
                    if isinstance(entry_id, str) and entry_id in entries:
                        existing = entries[entry_id]
                        entries[entry_id] = [
                            location_parent_id(existing),
                            location_kind(existing),
                            location_offset(existing),
                            location_length(existing),
                            offset,
                            len(line),
                        ]
                elif payload_type == SESSION_ENTRY_EVENT:
                    # Non-canonical field ordering fell through the fast
                    # extractor. Decode it fully rather than rejecting it.
                    raw_entry = payload.get("entry")
                    if not isinstance(raw_entry, dict):
                        return None
                    entry = ConversationEntry.model_validate(raw_entry)
                    existing = entries.get(entry.id)
                    entries[entry.id] = [
                        entry.parent_id,
                        entry.kind,
                        offset,
                        len(line),
                        metadata_offset(existing),
                        metadata_length(existing),
                    ]
                    scanned_entry_ids.append(entry.id)
    except OSError:
        return None
    if not saw_header:
        return None
    mark_legacy_tool_context_locations(
        source,
        entries,
        candidate_ids=scanned_entry_ids if prior is not None else None,
    )
    return MessageIndexSnapshot(
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        indexed_size=indexed_size,
        prefix_hash=prefix_hash,
        leaf_id=leaf_id,
        entries=entries,
    )
