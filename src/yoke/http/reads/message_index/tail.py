"""Bounded reads of active context and tree tails."""

from __future__ import annotations

import mmap
from typing import cast

from pydantic_core import from_json

from yoke.agent.tool_context import normalize_legacy_tool_context_entries
from yoke.http.reads.message_index.models import (
    UNSET,
    ContextIndexWindow,
    MessageIndexSnapshot,
    TreeIndexPage,
    entry_topology,
)
from yoke.http.reads.message_index.models import (
    parent_id as location_parent_id,
)
from yoke.http.reads.message_index.storage import IndexStorage
from yoke.http.reads.message_index.tail_navigation import indexed_leaf_id
from yoke.http.reads.message_index.tail_scan import (
    metadata_leaf,
    read_tail_entries,
    scan_tail,
)
from yoke.session import SessionStore
from yoke.session.repository.io import (
    SESSION_ENTRY_METADATA_EVENT,
    SESSION_METADATA_EVENT,
)


def has_persisted_tool_entries(store: SessionStore, session_id: str) -> bool | None:
    """Detect canonical persisted tool entries without building topology."""
    source = store.directory / f"{session_id}.jsonl"
    try:
        with source.open("rb") as handle:
            first_line = handle.readline()
            if not first_line.startswith(b'{"type":"yoke_session"'):
                return None
            stat = source.stat()
            if stat.st_size == 0:
                return False
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                return any(
                    mapped.find(marker) >= 0
                    for marker in (
                        b'{"type":"entry","entry":{"kind":"assistant_tool_calls"',
                        b'{"type":"entry","entry":{"kind":"tool_result"',
                    )
                )
    except OSError:
        return None


def tail_tree_page(
    store: SessionStore,
    storage: IndexStorage,
    session_id: str,
    *,
    limit: int,
) -> TreeIndexPage | None:
    """Read the newest tree rows without waiting for the full sidecar."""
    source = store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    scanned = _scan_tree_tail(source, stat.st_size, limit)
    if scanned is None:
        return None
    locations, newest_ids, metadata_events, explicit_leaf = scanned
    summary = store.index_entry(session_id)
    leaf_id = summary.leaf_id if summary is not None else None
    if explicit_leaf is not UNSET:
        leaf_id = explicit_leaf if isinstance(explicit_leaf, str) else None
    elif leaf_id is None and newest_ids:
        leaf_id = newest_ids[0]
    selected_ids = newest_ids[:limit]
    for entry_id in selected_ids:
        metadata = metadata_events.get(entry_id)
        if metadata is not None:
            locations[entry_id][4:] = metadata
    selected_ids.reverse()
    snapshot = MessageIndexSnapshot(
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        indexed_size=stat.st_size,
        prefix_hash="",
        leaf_id=leaf_id,
        entries=locations,
    )
    entries = storage.read_entries(session_id, snapshot, selected_ids)
    if entries is None:
        return None
    normalize_legacy_tool_context_entries(entries)
    active_ids = _active_tail_ids(locations, leaf_id)
    child_counts = _child_counts(locations, selected_ids)
    total_entries = (
        summary.entry_count
        if summary is not None and summary.entry_count is not None
        else len(selected_ids)
    )
    return TreeIndexPage(
        entries=entries,
        leaf_id=leaf_id,
        active_ids=frozenset(active_ids),
        child_counts=child_counts,
        total_entries=total_entries,
        has_more=len(newest_ids) > limit or total_entries > len(selected_ids),
    )


def _scan_tree_tail(
    source, source_size: int, limit: int
) -> (
    tuple[
        dict[str, list[object]],
        list[str],
        dict[str, tuple[int, int]],
        str | None | object,
    ]
    | None
):
    locations: dict[str, list[object]] = {}
    newest_ids: list[str] = []
    metadata_events: dict[str, tuple[int, int]] = {}
    explicit_leaf: str | None | object = UNSET

    def visit(raw: bytes, start: int, mapped: mmap.mmap) -> bool:
        nonlocal explicit_leaf
        topology = entry_topology(raw)
        if topology is not None:
            entry_id, parent_id, kind = topology
            locations[entry_id] = [
                parent_id,
                kind,
                start,
                len(raw),
                None,
                None,
            ]
            newest_ids.append(entry_id)
            if len(newest_ids) > limit:
                return True
            return False
        if not raw.startswith(b'{"type":'):
            return False
        try:
            payload = from_json(raw)
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        payload_type = payload.get("type")
        if payload_type == SESSION_METADATA_EVENT and explicit_leaf is UNSET:
            value = payload.get("leaf_id")
            if value is None or isinstance(value, str):
                explicit_leaf = value
        elif payload_type == SESSION_ENTRY_METADATA_EVENT:
            entry_id = payload.get("entry_id")
            if isinstance(entry_id, str) and entry_id not in metadata_events:
                metadata_events[entry_id] = (start, len(raw))

        return False

    if not scan_tail(source, source_size, visit):
        return None

    return locations, newest_ids, metadata_events, explicit_leaf


def _active_tail_ids(
    locations: dict[str, list[object]], leaf_id: str | None
) -> set[str]:
    active: set[str] = set()
    current = leaf_id
    while current is not None and current in locations:
        active.add(current)
        current = location_parent_id(locations[current])
    return active


def _child_counts(
    locations: dict[str, list[object]], selected_ids: list[str]
) -> dict[str, int]:
    counts = {entry_id: 0 for entry_id in selected_ids}
    for location in locations.values():
        parent = location_parent_id(location)
        if parent in counts:
            counts[parent] += 1
    return counts


def tail_context_window(
    store: SessionStore,
    session_id: str,
    *,
    limit: int,
    include_instructions: bool,
) -> ContextIndexWindow | None:
    """Serve recent context without waiting for a full topology build."""
    source = store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    leaf_id = indexed_leaf_id(store, session_id, stat.st_size, stat.st_mtime_ns)
    scanned = _scan_context_tail(
        source, stat.st_size, leaf_id, limit, include_instructions
    )
    if scanned is None:
        return None
    raw_entries, selected, complete = scanned
    entries = read_tail_entries(
        source, [(offset, length) for _, offset, length in selected]
    )
    if entries is None:
        return None
    summary = store.index_entry(session_id)
    total_entries = (
        summary.entry_count
        if summary is not None and summary.entry_count is not None
        else len(entries)
    )
    return ContextIndexWindow(
        entries=entries,
        total_entries=total_entries,
        truncated=not complete or total_entries > len(entries),
    )


def _scan_context_tail(source, size, leaf_id, limit, include_instructions):
    raw_entries: dict[str, tuple[str | None, str, int, int]] = {}
    selected: list[tuple[str, int, int]] = []
    current = leaf_id
    complete = False

    def consume() -> bool:
        nonlocal current, complete
        while current is not None:
            current_id = cast(str, current)
            item = raw_entries.get(current_id)
            if item is None:
                return False
            parent_id, kind, offset, length = item
            if include_instructions or kind != "instruction":
                selected.append((current_id, offset, length))
                if len(selected) > limit:
                    return True
            current = parent_id
        complete = True
        return True

    def visit(raw: bytes, start: int, mapped: mmap.mmap) -> bool:
        nonlocal leaf_id, current
        topology = entry_topology(raw)
        if topology is not None:
            entry_id, parent_id, kind = topology
            raw_entries[entry_id] = (parent_id, kind, start, len(raw))
            if leaf_id is None:
                leaf_id = entry_id
                current = entry_id
            if current == entry_id and consume():
                return True
            return False
        if leaf_id is None and raw.startswith(b'{"type":"metadata"'):
            value = metadata_leaf(raw)
            if value is not UNSET:
                leaf_id = value
                current = value
                if consume():
                    return True

        return False

    if not scan_tail(source, size, visit):
        return None
    if size == 0:
        return raw_entries, selected, True
    if current is not None and len(selected) <= limit:
        return None

    return raw_entries, selected[:limit][::-1], complete
