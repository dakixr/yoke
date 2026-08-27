"""Extracted helpers for the persistent HTTP session message index."""

from __future__ import annotations

import mmap
from typing import Any

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.http.services.session_message_index_models import MessageIndexSnapshot
from yoke.http.services.session_message_index_models import MessagePage
from yoke.http.services.session_message_index_models import NavigationIndexPreview
from yoke.http.services.session_message_index_models import PUBLIC_EXCLUDED_KINDS
from yoke.http.services.session_message_index_models import entry_topology
from yoke.http.services.session_message_index_models import known_parent_chain


def tail_navigation_preview(
    host: Any,
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
    entries = host._read_entries(session_id, snapshot, read_ids)
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
    host: Any,
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
    host: Any,
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
    leaf_id = host._indexed_leaf_id(session_id, stat.st_size, stat.st_mtime_ns)
    raw_entries: dict[str, tuple[str | None, str, int, int]] = {}
    selected: list[tuple[int, int]] = []
    current = leaf_id
    passed_anchor = anchor_id is None

    def consume_available() -> bool:
        nonlocal current, passed_anchor
        while current is not None:
            item = raw_entries.get(current)
            if item is None:
                return False
            parent_id, kind, offset, length = item
            if not passed_anchor:
                if current == anchor_id:
                    passed_anchor = True
            elif kind not in PUBLIC_EXCLUDED_KINDS:
                selected.append((offset, length))
                if len(selected) > limit:
                    return True
            current = parent_id
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


def indexed_leaf_id(
    host: Any,
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
