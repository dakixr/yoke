"""Cold-tail reads and indexed targets for session-tree navigation."""

from __future__ import annotations

import mmap
from typing import TYPE_CHECKING

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import normalize_legacy_tool_context_entries
from yoke.http.services.session_message_index import paths as paths
from yoke.http.services.session_message_index.models import MessageIndexSnapshot
from yoke.http.services.session_message_index.models import MessagePage
from yoke.http.services.session_message_index.models import NavigationIndexPreview
from yoke.http.services.session_message_index.models import PUBLIC_EXCLUDED_KINDS
from yoke.http.services.session_message_index.models import entry_topology
from yoke.http.services.session_message_index.models import known_parent_chain
from yoke.http.services.session_message_index.models import (
    parent_id as location_parent_id,
)
from yoke.http.services.session_message_index.storage import current_snapshot
from yoke.http.services.session_message_index.storage import read_entries

if TYPE_CHECKING:
    from yoke.http.services.session_message_index import SessionMessageIndex


def query_entry_tree_state(
    host: SessionMessageIndex,
    session_id: str,
    entry_id: str,
) -> tuple[ConversationEntry, bool, bool, int] | None:
    """Read one entry with its active/current and child-count state."""
    snapshot = host._ensure(session_id)
    if snapshot is None or entry_id not in snapshot.entries:
        return None
    entries = read_entries(host, session_id, snapshot, [entry_id])
    if not entries:
        return None
    active_ids = paths.active_ids(snapshot)
    if active_ids is None:
        return None
    child_count = sum(
        location_parent_id(location) == entry_id
        for location in snapshot.entries.values()
    )
    return (
        entries[0],
        entry_id in set(active_ids),
        entry_id == snapshot.leaf_id,
        child_count,
    )


def query_navigation_target(
    host: SessionMessageIndex,
    session_id: str,
    target_id: str,
) -> ConversationEntry | None:
    """Return one proven persisted navigation target from the topology index."""
    snapshot = current_snapshot(host, session_id)
    if snapshot is None:
        entry = tail_entry(host, session_id, target_id)
        if entry is not None:
            host.warm_async(session_id)
            return entry
        snapshot = host._ensure(session_id)
    if snapshot is None or target_id not in snapshot.entries:
        return None
    entries = read_entries(host, session_id, snapshot, [target_id])
    return entries[0] if entries else None


def tail_navigation_preview(
    host: SessionMessageIndex,
    session_id: str,
    *,
    target_id: str,
    abandoned_limit: int,
) -> NavigationIndexPreview | None:
    """Resolve one navigation preview from the relevant log tail only."""
    source = host.store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    summary = host.store.index_entry(session_id)
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

    try:
        with source.open("rb") as handle:
            if stat.st_size == 0:
                return None
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
                        locations[entry_id] = [
                            parent_id,
                            kind,
                            start,
                            len(raw),
                            None,
                            None,
                        ]
                    elif not leaf_known and raw.startswith(b'{"type":"metadata"'):
                        try:
                            payload = from_json(raw)
                        except ValueError:
                            continue
                        if isinstance(payload, dict) and "leaf_id" in payload:
                            value = payload.get("leaf_id")
                            if value is None or isinstance(value, str):
                                leaf_id = value
                                leaf_known = True
                    if not leaf_known or leaf_id is None or target_id not in locations:
                        continue
                    current_chain = known_parent_chain(locations, leaf_id)
                    target_chain = known_parent_chain(locations, target_id)
                    current_positions = {
                        entry_id: index for index, entry_id in enumerate(current_chain)
                    }
                    common_id = next(
                        (
                            entry_id
                            for entry_id in target_chain
                            if entry_id in current_positions
                        ),
                        None,
                    )
                    if common_id is not None:
                        break
    except (OSError, ValueError):
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
    entries = read_entries(host, session_id, snapshot, read_ids)
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
    host: SessionMessageIndex,
    session_id: str,
    entry_id: str,
) -> ConversationEntry | None:
    """Read one canonical entry by scanning backward without topology locking."""
    source = host.store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
        with source.open("rb") as handle:
            if stat.st_size == 0:
                return None
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
                    if topology is None or topology[0] != entry_id:
                        continue
                    payload = from_json(raw)
                    if not isinstance(payload, dict):
                        return None
                    raw_entry = payload.get("entry")
                    return (
                        ConversationEntry.model_validate(raw_entry)
                        if isinstance(raw_entry, dict)
                        else None
                    )
    except (OSError, ValueError):
        return None
    return None


def tail_page(
    host: SessionMessageIndex,
    session_id: str,
    *,
    limit: int,
    anchor_id: str | None,
) -> MessagePage | None:
    """Serve a descending active-branch page without scanning old bodies."""
    source = host.store.directory / f"{session_id}.jsonl"
    try:
        stat = source.stat()
    except OSError:
        return None
    leaf_id = indexed_leaf_id(host, session_id, stat.st_size, stat.st_mtime_ns)
    raw_entries: dict[str, tuple[str | None, str, int, int]] = {}
    selected: list[tuple[int, int]] = []
    current = leaf_id
    waiting_for = current
    passed_anchor = anchor_id is None

    def consume_available(mapped: mmap.mmap) -> bool:
        nonlocal current, passed_anchor, waiting_for
        while current is not None:
            item = raw_entries.get(current)
            if item is None:
                waiting_for = current
                return False
            parent_id, kind, offset, length = item
            if not passed_anchor:
                if current == anchor_id:
                    passed_anchor = True
            else:
                normalized_kind, missing_id = _tail_entry_kind(
                    mapped,
                    raw_entries,
                    current,
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

    try:
        with source.open("rb") as handle:
            if stat.st_size == 0:
                return MessagePage(entries=[], has_more=False)
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
                            # Canonical writes append leaf metadata before
                            # appended entries. If no newer metadata event
                            # appeared after the entries, the last entry is
                            # the current leaf. This makes old/missing index
                            # migrations fast without guessing across a
                            # metadata-only checkout.
                            leaf_id = entry_id
                            current = entry_id
                            waiting_for = entry_id
                        if waiting_for == entry_id and consume_available(mapped):
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
                                waiting_for = value
                                if consume_available(mapped):
                                    break
                if current is not None and len(selected) <= limit:
                    return None
    except (OSError, ValueError):
        return None

    if anchor_id is not None and not passed_anchor:
        return None
    page_locations = selected[:limit]
    entries: list[ConversationEntry] = []
    try:
        with source.open("rb") as handle:
            for offset, length in page_locations:
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
    return MessagePage(entries=entries, has_more=len(selected) > limit)


def _tail_entry_kind(
    mapped: mmap.mmap,
    raw_entries: dict[str, tuple[str | None, str, int, int]],
    entry_id: str,
) -> tuple[str | None, str | None]:
    """Classify one cold-tail user after its provenance chain is available."""
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
    host: SessionMessageIndex,
    session_id: str,
    source_size: int,
    source_mtime_ns: int,
) -> str | None:
    entry = host.store.index_entry(session_id)
    if entry is None:
        return None
    if entry.file_size != source_size or entry.file_mtime_ns != source_mtime_ns:
        return None
    return entry.leaf_id
