"""Bounded reads of active context and tree tails."""

from __future__ import annotations

import mmap
from typing import TYPE_CHECKING

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import normalize_legacy_tool_context_entries
from yoke.cli.session.io import SESSION_ENTRY_METADATA_EVENT
from yoke.cli.session.io import SESSION_METADATA_EVENT
from yoke.http.services.session_message_index.models import ContextIndexWindow
from yoke.http.services.session_message_index.models import MessageIndexSnapshot
from yoke.http.services.session_message_index.models import TreeIndexPage
from yoke.http.services.session_message_index.models import UNSET
from yoke.http.services.session_message_index.models import entry_topology
from yoke.http.services.session_message_index.models import (
    parent_id as location_parent_id,
)
from yoke.http.services.session_message_index.storage import read_entries
from yoke.http.services.session_message_index.tail_navigation import indexed_leaf_id

if TYPE_CHECKING:
    from yoke.http.services.session_message_index import SessionMessageIndex


def has_persisted_tool_entries(
    host: SessionMessageIndex, session_id: str
) -> bool | None:
    """Detect canonical persisted tool entries without building topology."""
    source = host.store.directory / f"{session_id}.jsonl"
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
    host: SessionMessageIndex,
    session_id: str,
    *,
    limit: int,
) -> TreeIndexPage | None:
    """Read the newest tree rows without waiting for the full sidecar."""
    source = host.store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    summary = host.store.index_entry(session_id)
    leaf_id = summary.leaf_id if summary is not None else None
    explicit_leaf: str | None | object = UNSET
    locations: dict[str, list[object]] = {}
    newest_ids: list[str] = []
    metadata_events: dict[str, tuple[int, int]] = {}

    try:
        with source.open("rb") as handle:
            if stat.st_size == 0:
                return TreeIndexPage(
                    entries=[],
                    leaf_id=None,
                    active_ids=frozenset(),
                    child_counts={},
                    total_entries=0,
                    has_more=False,
                )
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                end = stat.st_size
                if end and mapped[end - 1] == 10:
                    end -= 1
                while end > 0 and len(newest_ids) <= limit:
                    newline = mapped.rfind(b"\n", 0, end)
                    start = newline + 1
                    raw = mapped[start:end]
                    end = newline if newline >= 0 else 0
                    if not raw:
                        continue
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
                        continue
                    if not raw.startswith(b'{"type":'):
                        continue
                    try:
                        payload = from_json(raw)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    payload_type = payload.get("type")
                    if (
                        payload_type == SESSION_METADATA_EVENT
                        and explicit_leaf is UNSET
                    ):
                        if "leaf_id" in payload:
                            value = payload.get("leaf_id")
                            if value is None or isinstance(value, str):
                                explicit_leaf = value
                    elif payload_type == SESSION_ENTRY_METADATA_EVENT:
                        entry_id = payload.get("entry_id")
                        if (
                            isinstance(entry_id, str)
                            and entry_id not in metadata_events
                        ):
                            metadata_events[entry_id] = (start, len(raw))
    except (OSError, ValueError):
        return None

    has_more = len(newest_ids) > limit
    selected_ids = newest_ids[:limit]
    if explicit_leaf is not UNSET:
        leaf_id = explicit_leaf if isinstance(explicit_leaf, str) else None
    elif leaf_id is None and newest_ids:
        leaf_id = newest_ids[0]

    for entry_id in selected_ids:
        metadata = metadata_events.get(entry_id)
        if metadata is not None:
            locations[entry_id][4] = metadata[0]
            locations[entry_id][5] = metadata[1]
    selected_ids.reverse()
    snapshot = MessageIndexSnapshot(
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        indexed_size=stat.st_size,
        prefix_hash="",
        leaf_id=leaf_id,
        entries=locations,
    )
    entries = read_entries(host, session_id, snapshot, selected_ids)
    if entries is None:
        return None
    normalize_legacy_tool_context_entries(entries)

    active_ids: set[str] = set()
    current = leaf_id
    while current is not None and current in locations:
        active_ids.add(current)
        current = location_parent_id(locations[current])
    child_counts = {entry_id: 0 for entry_id in selected_ids}
    for location in locations.values():
        entry_parent_id = location_parent_id(location)
        if entry_parent_id in child_counts:
            child_counts[entry_parent_id] += 1
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
        has_more=has_more or total_entries > len(selected_ids),
    )


def tail_context_window(
    host: SessionMessageIndex,
    session_id: str,
    *,
    limit: int,
    include_instructions: bool,
) -> ContextIndexWindow | None:
    """Serve recent context without waiting for a full topology build."""
    source = host.store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    leaf_id = indexed_leaf_id(host, session_id, stat.st_size, stat.st_mtime_ns)
    raw_entries: dict[str, tuple[str | None, str, int, int]] = {}
    selected: list[tuple[str, int, int]] = []
    current = leaf_id
    complete = False

    def consume_available() -> bool:
        nonlocal current, complete
        while current is not None:
            item = raw_entries.get(current)
            if item is None:
                return False
            parent_id, kind, offset, length = item
            if include_instructions or kind != "instruction":
                selected.append((current, offset, length))
                if len(selected) > limit:
                    return True
            current = parent_id
        complete = True
        return True

    try:
        with source.open("rb") as handle:
            if stat.st_size == 0:
                return ContextIndexWindow(
                    entries=[],
                    total_entries=0,
                    truncated=False,
                )
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                end = stat.st_size
                if end and mapped[end - 1] == 10:
                    end -= 1
                while end > 0:
                    newline = mapped.rfind(b"\n", 0, end)
                    start = newline + 1
                    raw = mapped[start:end]
                    end = newline if newline >= 0 else 0
                    if not raw:
                        continue
                    topology = entry_topology(raw)
                    if topology is not None:
                        entry_id, parent_id, kind = topology
                        raw_entries[entry_id] = (
                            parent_id,
                            kind,
                            start,
                            len(raw),
                        )
                        if leaf_id is None:
                            leaf_id = entry_id
                            current = entry_id
                        if current == entry_id and consume_available():
                            break
                        continue
                    if leaf_id is None and raw.startswith(b'{"type":"metadata"'):
                        try:
                            payload = from_json(raw)
                        except ValueError:
                            continue
                        if isinstance(payload, dict) and "leaf_id" in payload:
                            value = payload.get("leaf_id")
                            if value is None or isinstance(value, str):
                                leaf_id = value
                                current = value
                                if consume_available():
                                    break
                if current is not None and len(selected) <= limit:
                    return None
    except (OSError, ValueError):
        return None

    selected = selected[:limit]
    selected.reverse()
    entries: list[ConversationEntry] = []
    try:
        with source.open("rb") as handle:
            for _entry_id, offset, length in selected:
                handle.seek(offset)
                payload = from_json(handle.read(length))
                if not isinstance(payload, dict):
                    return None
                raw_entry = payload.get("entry")
                if not isinstance(raw_entry, dict):
                    return None
                entries.append(ConversationEntry.model_validate(raw_entry))
    except (OSError, ValueError):
        return None

    summary = host.store.index_entry(session_id)
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
