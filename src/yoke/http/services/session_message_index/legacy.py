"""Legacy provenance repair for the persistent HTTP message index."""

from __future__ import annotations

from pathlib import Path

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import normalize_legacy_tool_context_entries
from yoke.http.services.session_message_index.models import kind
from yoke.http.services.session_message_index.models import length
from yoke.http.services.session_message_index.models import offset
from yoke.http.services.session_message_index.models import parent_id


def mark_legacy_tool_context_locations(
    source: Path,
    locations: dict[str, list[object]],
    *,
    candidate_ids: list[str] | None = None,
) -> None:
    """Mark legacy tool-injected image entries as tool context in the sidecar."""
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
    try:
        with source.open("rb") as handle:
            cache: dict[str, ConversationEntry | None] = {}

            def read(entry_id: str) -> ConversationEntry | None:
                if entry_id in cache:
                    return cache[entry_id]
                location = locations.get(entry_id)
                if location is None:
                    cache[entry_id] = None
                    return None
                handle.seek(offset(location))
                try:
                    payload = from_json(handle.read(length(location)).strip())
                    raw_entry = (
                        payload.get("entry") if isinstance(payload, dict) else None
                    )
                    entry = (
                        ConversationEntry.model_validate(raw_entry)
                        if isinstance(raw_entry, dict)
                        else None
                    )
                except (ValueError, OSError):
                    entry = None
                cache[entry_id] = entry
                return entry

            for candidate_id in candidates:
                ancestry = _read_ancestry(locations, read, candidate_id)
                if ancestry is None:
                    continue
                normalize_legacy_tool_context_entries(ancestry)
                if ancestry[0].kind == "tool_context":
                    locations[candidate_id][1] = "tool_context"
    except OSError:
        return


def _read_ancestry(
    locations: dict[str, list[object]],
    read,
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
