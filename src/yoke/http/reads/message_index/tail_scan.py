"""One bounded reverse scanner for canonical session tails."""

import mmap
from collections.abc import Callable
from pathlib import Path

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.http.reads.message_index.models import UNSET


def scan_tail(
    source: Path, size: int, visitor: Callable[[bytes, int, mmap.mmap], bool]
) -> bool:
    """Visit newest lines first until requested to stop; report unreadable tails."""
    try:
        with source.open("rb") as handle:
            if size == 0:
                return True
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                end = size - 1 if mapped[size - 1] == 10 else size
                while end > 0:
                    newline = mapped.rfind(b"\n", 0, end)
                    start = newline + 1
                    raw = mapped[start:end]
                    end = newline if newline >= 0 else 0
                    if raw and visitor(raw, start, mapped):
                        break
    except (OSError, ValueError):
        return False
    return True


def metadata_leaf(raw: bytes) -> str | None | object:
    """Return a valid explicit leaf selection, or UNSET when absent."""
    try:
        payload = from_json(raw)
    except ValueError:
        return UNSET
    if not isinstance(payload, dict) or "leaf_id" not in payload:
        return UNSET
    value = payload.get("leaf_id")
    return value if value is None or isinstance(value, str) else UNSET


def read_tail_entries(source, locations):
    entries: list[ConversationEntry] = []
    try:
        with source.open("rb") as handle:
            for offset, length in locations:
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
    return entries
