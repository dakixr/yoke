"""Extracted helpers for the persistent HTTP session message index."""

from __future__ import annotations

from typing import Any


from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.http.services.session_message_index_models import ContextIndexWindow
from yoke.http.services.session_message_index_models import MessagePage
from yoke.http.services.session_message_index_models import NavigationIndexPreview
from yoke.http.services.session_message_index_models import PUBLIC_EXCLUDED_KINDS
from yoke.http.services.session_message_index_models import TreeIndexPage
from yoke.http.services.session_message_index_models import kind
from yoke.http.services.session_message_index_models import (
    parent_id as location_parent_id,
)


def query_page(
    host: Any,
    session_id: str,
    *,
    limit: int,
    order: str,
    anchor_id: str | None,
) -> MessagePage | None:
    snapshot = host._current_snapshot(session_id)
    if snapshot is None and order == "desc":
        page = host._tail_page(
            session_id,
            limit=limit,
            anchor_id=anchor_id,
        )
        if page is not None:
            host.warm_async(session_id)
            return page
    if snapshot is None:
        snapshot = host._ensure(session_id)
    if snapshot is None:
        return None
    if order == "desc":
        ids = host._descending_ids(snapshot, limit=limit, anchor_id=anchor_id)
    else:
        ids = host._ascending_ids(snapshot, limit=limit, anchor_id=anchor_id)
    if ids is None:
        return None
    selected, has_more = ids
    entries = host._read_entries(session_id, snapshot, selected)
    if entries is None:
        return None
    return MessagePage(entries=entries, has_more=has_more)


def query_entry(host: Any, session_id: str, entry_id: str) -> ConversationEntry | None:
    snapshot = host._ensure(session_id)
    if snapshot is None:
        return None
    location = snapshot.entries.get(entry_id)
    if location is None or kind(location) in PUBLIC_EXCLUDED_KINDS:
        return None
    entries = host._read_entries(session_id, snapshot, [entry_id])
    return entries[0] if entries else None


def query_tool_trace_messages(host: Any, session_id: str) -> list[Message] | None:
    """Read only active-branch turns that contain persisted tool activity."""
    snapshot = host._current_snapshot(session_id)
    if snapshot is None:
        has_tools = host._has_persisted_tool_entries(session_id)
        if has_tools is False:
            host.warm_async(session_id)
            return []
        snapshot = host._ensure(session_id)
    if snapshot is None:
        return None
    active_ids = host._active_ids(snapshot)
    if active_ids is None:
        return None
    tool_indexes = [
        index
        for index, entry_id in enumerate(active_ids)
        if kind(snapshot.entries[entry_id]) in {"assistant_tool_calls", "tool_result"}
    ]
    if not tool_indexes:
        return []

    selected: set[int] = set()
    for index in tool_indexes:
        start = index
        while start > 0:
            previouskind = kind(snapshot.entries[active_ids[start - 1]])
            start -= 1
            if previouskind == "user":
                break
        end = index + 1
        while end < len(active_ids):
            if kind(snapshot.entries[active_ids[end]]) == "user":
                break
            end += 1
        selected.update(range(start, end))

    entry_ids = [
        entry_id for index, entry_id in enumerate(active_ids) if index in selected
    ]
    entries = host._read_entries(session_id, snapshot, entry_ids)
    if entries is None:
        return None
    return [entry.message for entry in entries if entry.message is not None]


def query_tree_page(
    host: Any,
    session_id: str,
    *,
    limit: int,
    anchor_id: str | None,
) -> TreeIndexPage | None:
    """Read one newest-first tree page while keeping node rows chronological."""
    snapshot = host._current_snapshot(session_id)
    if snapshot is None and anchor_id is None:
        tail = host._tail_tree_page(session_id, limit=limit)
        if tail is not None:
            host.warm_async(session_id)
            return tail
    if snapshot is None:
        snapshot = host._ensure(session_id)
    if snapshot is None:
        return None
    ordered_ids = list(snapshot.entries)
    end = len(ordered_ids)
    if anchor_id is not None:
        try:
            end = ordered_ids.index(anchor_id)
        except ValueError:
            return None
    start = max(0, end - limit)
    selected_ids = ordered_ids[start:end]
    entries = host._read_entries(session_id, snapshot, selected_ids)
    if entries is None:
        return None
    active = host._active_ids(snapshot)
    if active is None:
        return None
    child_counts = {entry_id: 0 for entry_id in snapshot.entries}
    for entry_id, location in snapshot.entries.items():
        entry_parent_id = location_parent_id(location)
        if entry_parent_id in child_counts:
            child_counts[entry_parent_id] += 1
    return TreeIndexPage(
        entries=entries,
        leaf_id=snapshot.leaf_id,
        active_ids=frozenset(active),
        child_counts=child_counts,
        total_entries=len(ordered_ids),
        has_more=start > 0,
    )


def query_context_window(
    host: Any,
    session_id: str,
    *,
    limit: int,
    include_instructions: bool,
) -> ContextIndexWindow | None:
    """Read a bounded active context tail and the latest checkpoint if present."""
    snapshot = host._current_snapshot(session_id)
    if snapshot is None:
        tail = host._tail_context_window(
            session_id,
            limit=limit,
            include_instructions=include_instructions,
        )
        if tail is not None:
            host.warm_async(session_id)
            return tail
        snapshot = host._ensure(session_id)
    if snapshot is None:
        return None
    active_ids = host._active_ids(snapshot)
    if active_ids is None:
        return None
    checkpoint_index = next(
        (
            index
            for index in range(len(active_ids) - 1, -1, -1)
            if kind(snapshot.entries[active_ids[index]]) == "memory_snapshot"
        ),
        None,
    )
    if checkpoint_index is None:
        selected_ids = active_ids[-limit:] if limit else []
        truncated = len(selected_ids) < len(active_ids)
    else:
        continuation = active_ids[checkpoint_index + 1 :]
        selected_ids = [active_ids[checkpoint_index]]
        if limit:
            selected_ids.extend(continuation[-limit:])
        truncated = len(selected_ids) < len(active_ids)

    if include_instructions:
        instruction_ids = [
            entry_id
            for entry_id in active_ids
            if kind(snapshot.entries[entry_id]) == "instruction"
        ]
        selected = set(selected_ids)
        selected_ids = [
            *[entry_id for entry_id in instruction_ids if entry_id not in selected],
            *selected_ids,
        ]
        truncated = len(set(selected_ids)) < len(active_ids)
    entries = host._read_entries(session_id, snapshot, selected_ids)
    if entries is None:
        return None
    return ContextIndexWindow(
        entries=entries,
        total_entries=len(active_ids),
        truncated=truncated,
    )


def query_navigation_preview(
    host: Any,
    session_id: str,
    *,
    target_id: str,
    abandoned_limit: int,
) -> NavigationIndexPreview | None:
    """Compare current and target parent chains without loading full entries."""
    snapshot = host._current_snapshot(session_id)
    if snapshot is None:
        tail = host._tail_navigation_preview(
            session_id,
            target_id=target_id,
            abandoned_limit=abandoned_limit,
        )
        if tail is not None:
            host.warm_async(session_id)
            return tail
        snapshot = host._ensure(session_id)
    if snapshot is None or target_id not in snapshot.entries:
        return None
    current_ids = host._active_ids(snapshot)
    target_ids = host._path_to(snapshot, target_id)
    if current_ids is None or target_ids is None:
        return None
    common_count = 0
    for current, target in zip(current_ids, target_ids, strict=False):
        if current != target:
            break
        common_count += 1
    abandoned_ids = current_ids[common_count:]
    selected_abandoned_ids = abandoned_ids[-abandoned_limit:] if abandoned_limit else []
    read_ids = [target_id, *selected_abandoned_ids]
    entries = host._read_entries(session_id, snapshot, read_ids)
    if entries is None or not entries:
        return None
    return NavigationIndexPreview(
        target=entries[0],
        current=target_id == snapshot.leaf_id,
        abandoned=entries[1:],
        abandoned_total=len(abandoned_ids),
        abandoned_truncated=len(selected_abandoned_ids) < len(abandoned_ids),
    )


def query_entry_tree_state(
    host: Any,
    session_id: str,
    entry_id: str,
) -> tuple[ConversationEntry, bool, bool, int] | None:
    """Read one entry with its active/current and child-count state."""
    snapshot = host._ensure(session_id)
    if snapshot is None or entry_id not in snapshot.entries:
        return None
    entries = host._read_entries(session_id, snapshot, [entry_id])
    if not entries:
        return None
    active_ids = host._active_ids(snapshot)
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
    host: Any,
    session_id: str,
    target_id: str,
) -> ConversationEntry | None:
    """Return one proven persisted navigation target from the topology index."""
    snapshot = host._current_snapshot(session_id)
    if snapshot is None:
        entry = host._tail_entry(session_id, target_id)
        if entry is not None:
            host.warm_async(session_id)
            return entry
        snapshot = host._ensure(session_id)
    if snapshot is None or target_id not in snapshot.entries:
        return None
    entries = host._read_entries(session_id, snapshot, [target_id])
    return entries[0] if entries else None
