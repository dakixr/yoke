"""Bounded active-branch reads for portable session handoffs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.cli.session.store import SessionStore

_ENTRY_PREFIX = b'{"type":"entry","entry":{"kind":'
_MESSAGE_MARKER = b',"message":'
_ID_MARKER = b',"id":'
_PARENT_MARKER = b',"parent_id":'
_CREATED_MARKER = b',"created_at":'
_SCAN_CHUNK = 1 << 20
_TAIL_BYTES = 1 << 12


@dataclass(frozen=True, slots=True)
class HandoffContextWindow:
    """Bounded active entries plus lightweight source metadata."""

    entries: list[ConversationEntry]
    total_entries: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class _EntryLocation:
    entry_id: str
    parent_id: str | None
    kind: str
    offset: int
    length: int


def read_handoff_context_window(
    store: SessionStore,
    session_id: str,
    *,
    recent_limit: int,
) -> HandoffContextWindow | None:
    """Read the latest checkpoint and recent active tail without full session decode."""
    summary = store.index_entry(session_id)
    if summary is None or not summary.leaf_id:
        return None
    source = store.directory / f"{session_id}.jsonl"
    try:
        source_size = source.stat().st_size
    except OSError:
        return None

    recent: list[_EntryLocation] = []
    checkpoint: _EntryLocation | None = None
    current: str | None = summary.leaf_id
    active_seen = 0
    with source.open("rb") as handle:
        for start, end in _reverse_line_ranges(handle, source_size):
            if current is None:
                break
            topology = _entry_topology(handle, start, end)
            if topology is None or topology.entry_id != current:
                continue
            active_seen += 1
            current = topology.parent_id
            if topology.kind == "memory_snapshot":
                checkpoint = topology
                break
            if len(recent) < recent_limit:
                recent.append(topology)

        # If the indexed leaf could not be found, fall back to the canonical loader.
        if active_seen == 0:
            return None

        selected = list(reversed(recent))
        if checkpoint is not None:
            selected.insert(0, checkpoint)
        entries = [_read_entry(handle, location) for location in selected]

    total_entries = summary.entry_count or active_seen
    return HandoffContextWindow(
        entries=entries,
        total_entries=total_entries,
        truncated=(
            checkpoint is not None or active_seen > len(recent) or current is not None
        ),
    )


def _reverse_line_ranges(
    handle: BinaryIO,
    size: int,
):
    """Yield JSONL byte ranges newest-first with fixed memory usage."""
    line_end = size
    if size:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            line_end -= 1
    cursor = line_end
    while cursor > 0:
        chunk_start = max(0, cursor - _SCAN_CHUNK)
        handle.seek(chunk_start)
        chunk = handle.read(cursor - chunk_start)
        search_end = len(chunk)
        while search_end:
            newline = chunk.rfind(b"\n", 0, search_end)
            if newline < 0:
                break
            line_start = chunk_start + newline + 1
            if line_start < line_end:
                yield line_start, line_end
            line_end = chunk_start + newline
            search_end = newline
        cursor = chunk_start
    if line_end > 0:
        yield 0, line_end


def _entry_topology(
    handle: BinaryIO,
    start: int,
    end: int,
) -> _EntryLocation | None:
    length = end - start
    if length <= len(_ENTRY_PREFIX):
        return None
    handle.seek(start)
    prefix = handle.read(min(length, 512))
    if not prefix.startswith(_ENTRY_PREFIX):
        return None
    message_at = prefix.find(_MESSAGE_MARKER, len(_ENTRY_PREFIX))
    if message_at < 0:
        return None
    kind = _decode_json_string(prefix[len(_ENTRY_PREFIX) : message_at])
    if kind is None:
        return None

    tail_start = max(start, end - _TAIL_BYTES)
    handle.seek(tail_start)
    tail = handle.read(end - tail_start)
    id_at = tail.rfind(_ID_MARKER)
    parent_at = tail.rfind(_PARENT_MARKER)
    created_at = tail.rfind(_CREATED_MARKER)
    if min(id_at, parent_at, created_at) < 0 or not id_at < parent_at < created_at:
        return None
    entry_id = _decode_json_string(tail[id_at + len(_ID_MARKER) : parent_at])
    raw_parent = tail[parent_at + len(_PARENT_MARKER) : created_at]
    parent_id = None if raw_parent == b"null" else _decode_json_string(raw_parent)
    if entry_id is None or raw_parent != b"null" and parent_id is None:
        return None
    return _EntryLocation(
        entry_id=entry_id,
        parent_id=parent_id,
        kind=kind,
        offset=start,
        length=length,
    )


def _read_entry(handle: BinaryIO, location: _EntryLocation) -> ConversationEntry:
    handle.seek(location.offset)
    raw = handle.read(location.length)
    sanitized = _elide_embedded_image_data(raw)
    payload = from_json(sanitized)
    if not isinstance(payload, dict) or not isinstance(payload.get("entry"), dict):
        raise ValueError(f"Invalid session entry: {location.entry_id}")
    return ConversationEntry.model_validate(payload["entry"]).model_copy(
        update={"kind": location.kind}
    )


def _elide_embedded_image_data(raw: bytes) -> bytes:
    """Remove inline image bytes before Pydantic parses a selected entry."""
    replacements = (
        (b'"url":"data:', b'"url":"data:"'),
        (b'"data_url":"data:', b'"data_url":null'),
    )
    result = raw
    for marker, replacement in replacements:
        search_from = 0
        parts: list[bytes] | None = None
        while True:
            marker_at = result.find(marker, search_from)
            if marker_at < 0:
                break
            value_start = marker_at + marker.find(b'"data:') + 1
            value_end = result.find(b'"', value_start)
            if value_end < 0:
                break
            if parts is None:
                parts = []
            parts.append(result[search_from:marker_at])
            parts.append(replacement)
            search_from = value_end + 1
        if parts is not None:
            parts.append(result[search_from:])
            result = b"".join(parts)
    return result


def _decode_json_string(value: bytes) -> str | None:
    if len(value) < 2 or value[0] != 34 or value[-1] != 34:
        return None
    try:
        return value[1:-1].decode("utf-8")
    except UnicodeDecodeError:
        return None
