"""Detached legacy compaction handoff recovery."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot

from ._topology import active_path


def reconnect_detached_handoff(
    entries: list[ConversationEntry],
    path: list[ConversationEntry],
    parse_summary: Callable[[str], str | None],
) -> tuple[list[ConversationEntry], bool]:
    """Reconnect nested legacy handoffs and remove retained-message copies."""
    positions = {entry.id: index for index, entry in enumerate(entries)}
    reconnected = False
    visited: set[str] = set()
    while _is_detached_handoff_root(path, parse_summary):
        root = path[0]
        if root.id in visited:
            break
        root_message = root.message
        if root_message is None:
            break
        visited.add(root.id)
        match = _matching_snapshot_before(
            entries,
            positions[root.id],
            parse_summary(root_message.plain_text_content or ""),
        )
        if match is None:
            break
        marker, snapshot = match
        continuation = _without_retained_prefix(path[1:], snapshot)
        path = [*active_path(entries, marker.id), *continuation]
        reconnected = True
    return (
        _without_replayed_handoff_prefixes(
            entries,
            path,
            positions,
            parse_summary,
        ),
        reconnected,
    )


def _is_detached_handoff_root(
    path: list[ConversationEntry],
    parse_summary: Callable[[str], str | None],
) -> bool:
    return bool(
        path
        and path[0].parent_id is None
        and path[0].message is not None
        and path[0].message.role == "user"
        and parse_summary(path[0].message.plain_text_content or "") is not None
    )


def _matching_snapshot_before(
    entries: list[ConversationEntry],
    boundary: int,
    summary: str | None,
) -> tuple[ConversationEntry, MemorySnapshot] | None:
    if summary is None:
        return None
    match: tuple[ConversationEntry, MemorySnapshot] | None = None
    for entry in entries[:boundary]:
        if entry.kind != "memory_snapshot":
            continue
        try:
            snapshot = MemorySnapshot.model_validate(entry.metadata)
        except ValueError:
            continue
        if snapshot.summary_text == summary:
            match = entry, snapshot
    return match


def _without_retained_prefix(
    continuation: list[ConversationEntry], snapshot: MemorySnapshot
) -> list[ConversationEntry]:
    handoff = snapshot.compaction_handoff
    retained = handoff.retained_messages if handoff is not None else []
    if not retained or len(continuation) < len(retained):
        return continuation
    if all(
        continuation[index].message == message for index, message in enumerate(retained)
    ):
        return continuation[len(retained) :]
    return continuation


def _without_replayed_handoff_prefixes(
    entries: list[ConversationEntry],
    path: list[ConversationEntry],
    positions: dict[str, int],
    parse_summary: Callable[[str], str | None],
) -> list[ConversationEntry]:
    repaired: list[ConversationEntry] = []
    index = 0
    while index < len(path):
        entry = path[index]
        message = entry.message
        summary = (
            parse_summary(message.plain_text_content or "")
            if message is not None and message.role == "user"
            else None
        )
        match = _matching_snapshot_before(
            entries,
            positions[entry.id],
            summary,
        )
        repaired.append(entry)
        if match is None:
            index += 1
            continue
        _, snapshot = match
        continuation = path[index + 1 :]
        stripped = _without_retained_prefix(continuation, snapshot)
        index += 1 + len(continuation) - len(stripped)
    return repaired
