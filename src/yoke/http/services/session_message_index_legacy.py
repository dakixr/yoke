"""Legacy provenance repair for the persistent HTTP message index."""

from __future__ import annotations

from pathlib import Path

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import TOOL_CONTEXT_TOOL_NAMES
from yoke.agent.tool_context import message_has_image
from yoke.http.services.session_message_index_models import kind
from yoke.http.services.session_message_index_models import length
from yoke.http.services.session_message_index_models import offset
from yoke.http.services.session_message_index_models import parent_id


def mark_legacy_tool_context_locations(
    source: Path,
    locations: dict[str, list[object]],
) -> None:
    """Mark legacy tool-injected image entries as tool context in the sidecar."""
    candidates = [
        entry_id
        for entry_id, location in locations.items()
        if kind(location) == "user"
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
                candidate = read(candidate_id)
                if candidate is None or not message_has_image(candidate.message):
                    continue
                parent = read(candidate.parent_id or "")
                if (
                    parent is None
                    or parent.kind != "tool_result"
                    or parent.message is None
                    or not parent.message.tool_call_id
                ):
                    continue
                if (
                    _ancestor_tool_name(
                        locations,
                        read,
                        parent.parent_id,
                        parent.message.tool_call_id,
                    )
                    in TOOL_CONTEXT_TOOL_NAMES
                ):
                    locations[candidate_id][1] = "tool_context"
    except OSError:
        return


def _ancestor_tool_name(
    locations: dict[str, list[object]],
    read,
    current_id: str | None,
    call_id: str,
) -> str | None:
    seen: set[str] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        location = locations.get(current_id)
        if location is None or kind(location) == "user":
            return None
        if kind(location) == "assistant_tool_calls":
            entry = read(current_id)
            if entry is not None and entry.message is not None:
                for call in entry.message.tool_calls:
                    if call.id == call_id:
                        return call.function.name
        current_id = parent_id(location)
    return None
