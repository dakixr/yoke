"""Legacy provenance repair for the HTTP message index."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import normalize_legacy_tool_context_entries
from yoke.http.reads.message_index.models import kind, parent_id

type EntryReader = Callable[[str], ConversationEntry | None]


def mark_legacy_tool_context_locations(
    read: EntryReader,
    locations: dict[str, list[object]],
    *,
    candidate_ids: list[str] | None = None,
) -> None:
    """Mark legacy tool-injected image entries as tool context in the index."""
    candidate_scope = set(candidate_ids) if candidate_ids is not None else None
    candidates = [
        entry_id
        for entry_id, location in locations.items()
        if (candidate_scope is None or entry_id in candidate_scope)
        and kind(location) == "user"
        and (parent := locations.get(parent_id(location) or "")) is not None
        and kind(parent) == "tool_result"
    ]
    if not candidates:
        return
    cache: dict[str, ConversationEntry | None] = {}

    def cached_read(entry_id: str) -> ConversationEntry | None:
        if entry_id not in cache:
            cache[entry_id] = read(entry_id) if entry_id in locations else None
        return cache[entry_id]

    for candidate_id in candidates:
        ancestry = _read_ancestry(locations, cached_read, candidate_id)
        if ancestry is None:
            continue
        normalize_legacy_tool_context_entries(ancestry)
        if ancestry[0].kind == "tool_context":
            locations[candidate_id][1] = "tool_context"


def _read_ancestry(
    locations: dict[str, list[object]],
    read: EntryReader,
    candidate_id: str,
) -> list[ConversationEntry] | None:
    """Read the finite provenance chain needed by the shared normalizer."""
    entries: list[ConversationEntry] = []
    seen: set[str] = set()
    current_id: str | None = candidate_id
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        location = locations.get(current_id)
        if location is None:
            return None
        entry = read(current_id)
        if entry is None:
            return None
        entries.append(entry)
        if current_id != candidate_id and kind(location) == "user":
            return entries
        current_id = parent_id(location)
    return entries if current_id is None else None
