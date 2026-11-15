"""Cold-tail reads and indexed targets for session-tree navigation."""

from __future__ import annotations

import mmap
from typing import cast

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import normalize_legacy_tool_context_entries
from yoke.http.reads.message_index import paths as paths
from yoke.http.reads.message_index.models import (
    PUBLIC_EXCLUDED_KINDS,
    MessageIndexSnapshot,
    MessagePage,
    NavigationIndexPreview,
    entry_topology,
    known_parent_chain,
)
from yoke.http.reads.message_index.storage import IndexStorage
from yoke.http.reads.message_index.tail_scan import (
    metadata_leaf,
    read_tail_entries,
    scan_tail,
)
from yoke.session import SessionStore


def tail_navigation_preview(
    store: SessionStore,
    storage: IndexStorage,
    session_id: str,
    *,
    target_id: str,
    abandoned_limit: int,
) -> NavigationIndexPreview | None:
    source = store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    summary = store.index_entry(session_id)
    summary_current = (
        summary is not None
        and summary.file_size == stat.st_size
        and summary.file_mtime_ns == stat.st_mtime_ns
    )
    leaf_id = summary.leaf_id if summary_current and summary is not None else None
    leaf_known = summary_current
    locations: dict[str, list[object]] = {}
    common_id: str | None = None
    current_chain: list[str] = []
    target_chain: list[str] = []

    def visit(raw: bytes, start: int, mapped: mmap.mmap) -> bool:
        nonlocal leaf_id, leaf_known, current_chain, target_chain, common_id
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
        elif not leaf_known and raw.startswith(b'{"type":"metadata"'):
            value = metadata_leaf(raw)
            if value is None or isinstance(value, str):
                leaf_id = value
                leaf_known = True
        if not leaf_known or leaf_id is None or target_id not in locations:
            return False
        current_chain = known_parent_chain(locations, leaf_id)
        target_chain = known_parent_chain(locations, target_id)
        current_positions = {
            entry_id: index for index, entry_id in enumerate(current_chain)
        }
        common_id = next(
            (entry_id for entry_id in target_chain if entry_id in current_positions),
            None,
        )
        if common_id is not None:
            return True

        return False

    if not scan_tail(source, stat.st_size, visit):
        return None

    if target_id not in locations or leaf_id is None or common_id is None:
        return None
    common_index = current_chain.index(common_id)
    abandoned_ids = list(reversed(current_chain[:common_index]))
    selected_abandoned_ids = abandoned_ids[-abandoned_limit:] if abandoned_limit else []
    read_ids = [target_id, *selected_abandoned_ids]
    snapshot = MessageIndexSnapshot(
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        indexed_size=stat.st_size,
        prefix_hash="",
        leaf_id=leaf_id,
        entries=locations,
    )
    entries = storage.read_entries(session_id, snapshot, read_ids)
    if not entries:
        return None
    return NavigationIndexPreview(
        target=entries[0],
        current=target_id == leaf_id,
        abandoned=entries[1:],
        abandoned_total=len(abandoned_ids),
        abandoned_truncated=len(selected_abandoned_ids) < len(abandoned_ids),
    )


def tail_entry(
    store: SessionStore,
    session_id: str,
    entry_id: str,
) -> ConversationEntry | None:
    """Read one canonical entry by scanning backward without topology locking."""
    source = store.directory / f"{session_id}.jsonl"
    found: ConversationEntry | None = None

    def visit(raw: bytes, start: int, mapped: mmap.mmap) -> bool:
        nonlocal found
        topology = entry_topology(raw)
        if topology is None or topology[0] != entry_id:
            return False
        payload = from_json(raw)
        raw_entry = payload.get("entry") if isinstance(payload, dict) else None
        if isinstance(raw_entry, dict):
            found = ConversationEntry.model_validate(raw_entry)
        return True

    try:
        size = source.stat().st_size
    except OSError:
        return None
    return found if scan_tail(source, size, visit) else None


def tail_page(
    store: SessionStore,
    session_id: str,
    *,
    limit: int,
    anchor_id: str | None,
) -> MessagePage | None:
    source = store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    leaf_id = indexed_leaf_id(store, session_id, stat.st_size, stat.st_mtime_ns)
    scanned = _scan_navigation_tail(source, stat.st_size, leaf_id, limit, anchor_id)
    if scanned is None:
        return None
    locations, selected, passed_anchor = scanned
    if anchor_id is not None and not passed_anchor:
        return None
    entries = read_tail_entries(source, selected[:limit])
    if entries is None:
        return None
    return MessagePage(entries=entries, has_more=len(selected) > limit)


def _scan_navigation_tail(source, size, leaf_id, limit, anchor_id):
    raw_entries: dict[str, tuple[str | None, str, int, int]] = {}
    selected: list[tuple[int, int]] = []
    current = leaf_id
    waiting_for = current
    passed_anchor = anchor_id is None

    def consume(mapped: mmap.mmap) -> bool:
        nonlocal current, passed_anchor, waiting_for
        while current is not None:
            current_id = cast(str, current)
            item = raw_entries.get(current_id)
            if item is None:
                waiting_for = current
                return False
            parent_id, _kind, offset, length = item
            if not passed_anchor:
                if current == anchor_id:
                    passed_anchor = True
            else:
                normalized_kind, missing_id = _tail_entry_kind(
                    mapped, raw_entries, current_id
                )
                if normalized_kind is None:
                    waiting_for = missing_id
                    return False
                if normalized_kind not in PUBLIC_EXCLUDED_KINDS:
                    selected.append((offset, length))
            if len(selected) > limit:
                return True
            current = parent_id
            waiting_for = current
        return True

    def visit(raw: bytes, start: int, mapped: mmap.mmap) -> bool:
        nonlocal leaf_id, current, waiting_for
        topology = entry_topology(raw)
        if topology is not None:
            entry_id, parent_id, kind = topology
            raw_entries[entry_id] = (parent_id, kind, start, len(raw))
            if leaf_id is None:
                leaf_id = entry_id
                current = entry_id
                waiting_for = entry_id
            if waiting_for == entry_id and consume(mapped):
                return True
            return False
        if leaf_id is None and raw.startswith(b'{"type":"metadata"'):
            value = metadata_leaf(raw)
            if value is None or isinstance(value, str):
                leaf_id = value
                current = value
                waiting_for = value
                if consume(mapped):
                    return True

        return False

    if not scan_tail(source, size, visit):
        return None
    if size == 0:
        return {}, [], passed_anchor
    if current is not None and len(selected) <= limit:
        return None

    return raw_entries, selected, passed_anchor


def _tail_entry_kind(
    mapped: mmap.mmap,
    raw_entries: dict[str, tuple[str | None, str, int, int]],
    entry_id: str,
) -> tuple[str | None, str | None]:
    location = raw_entries[entry_id]
    if location[1] != "user":
        return location[1], None
    chain: list[ConversationEntry] = []
    current: str | None = entry_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        item = raw_entries.get(current)
        if item is None:
            return None, current
        parent, item_kind, offset, length = item
        payload = from_json(mapped[offset : offset + length])
        raw_entry = payload.get("entry") if isinstance(payload, dict) else None
        if not isinstance(raw_entry, dict):
            raise ValueError("Invalid session entry")
        chain.append(ConversationEntry.model_validate(raw_entry))
        if current != entry_id and item_kind == "user":
            normalize_legacy_tool_context_entries(chain)
            return chain[0].kind, None
        current = parent
    if current is not None:
        return None, None
    normalize_legacy_tool_context_entries(chain)
    return chain[0].kind, None


def indexed_leaf_id(
    store: SessionStore,
    session_id: str,
    source_size: int,
    source_mtime_ns: int,
) -> str | None:
    entry = store.index_entry(session_id)
    return (
        entry.leaf_id
        if entry is not None
        and entry.file_size == source_size
        and entry.file_mtime_ns == source_mtime_ns
        else None
    )
