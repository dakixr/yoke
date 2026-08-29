"""Shared types and low-level helpers for HTTP session message indexing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry


INDEX_VERSION = 2
PUBLIC_EXCLUDED_KINDS = {"instruction", "memory_snapshot"}
UNSET = object()


@dataclass(frozen=True, slots=True)
class MessageIndexSnapshot:
    source_size: int
    source_mtime_ns: int
    indexed_size: int
    prefix_hash: str
    leaf_id: str | None
    entries: dict[str, list[object]]


@dataclass(frozen=True, slots=True)
class MessagePage:
    entries: list[ConversationEntry]
    has_more: bool


@dataclass(frozen=True, slots=True)
class TreeIndexPage:
    entries: list[ConversationEntry]
    leaf_id: str | None
    active_ids: frozenset[str]
    child_counts: dict[str, int]
    total_entries: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class ContextIndexWindow:
    entries: list[ConversationEntry]
    total_entries: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class RuntimeIndexSeed:
    """Compaction-bounded active entries sufficient to rebuild model state."""

    entries: list[ConversationEntry]
    leaf_id: str
    total_entries: int


@dataclass(frozen=True, slots=True)
class NavigationIndexPreview:
    target: ConversationEntry
    current: bool
    abandoned: list[ConversationEntry]
    abandoned_total: int
    abandoned_truncated: bool


def entry_topology(line: bytes) -> tuple[str, str | None, str] | None:
    prefix = b'{"type":"entry","entry":{"kind":'
    if not line.startswith(prefix):
        return None
    message_marker = b',"message":'
    id_marker = b',"id":'
    parent_marker = b',"parent_id":'
    created_marker = b',"created_at":'
    message_at = line.find(message_marker, len(prefix))
    id_at = line.rfind(id_marker)
    parent_at = line.rfind(parent_marker)
    created_at = line.rfind(created_marker)
    if min(message_at, id_at, parent_at, created_at) < 0:
        return None
    if not (message_at < id_at < parent_at < created_at):
        return None
    entry_kind = _decode_json_string(line[len(prefix) : message_at])
    entry_id = _decode_json_string(line[id_at + len(id_marker) : parent_at])
    raw_parent = line[parent_at + len(parent_marker) : created_at]
    entry_parent = _decode_json_string_or_none(raw_parent)
    if not isinstance(entry_kind, str) or not isinstance(entry_id, str):
        return None
    if raw_parent != b"null" and entry_parent is None:
        return None
    return entry_id, entry_parent, entry_kind


def _decode_json_string(value: bytes) -> str | None:
    if len(value) >= 2 and value[0] == 34 and value[-1] == 34 and b"\\" not in value:
        try:
            return value[1:-1].decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        decoded = from_json(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, str) else None


def _decode_json_string_or_none(value: bytes) -> str | None:
    if value == b"null":
        return None
    return _decode_json_string(value)


def prefix_hash(path: Path, size: int) -> str | None:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(min(size, 4096))
    except OSError:
        return None
    return hashlib.blake2b(prefix, digest_size=16).hexdigest()


def snapshot_matches(
    snapshot: MessageIndexSnapshot,
    size: int,
    mtime_ns: int,
    source_prefix_hash: str,
) -> bool:
    return (
        snapshot.indexed_size == size
        and snapshot.source_size == size
        and snapshot.source_mtime_ns == mtime_ns
        and snapshot.prefix_hash == source_prefix_hash
    )


def can_append(
    snapshot: MessageIndexSnapshot, size: int, source_prefix_hash: str
) -> bool:
    return (
        size >= snapshot.indexed_size
        and snapshot.indexed_size == snapshot.source_size
        and snapshot.prefix_hash == source_prefix_hash
    )


def parent_id(location: list[object]) -> str | None:
    value = location[0]
    return value if isinstance(value, str) else None


def kind(location: list[object]) -> str:
    value = location[1]
    return value if isinstance(value, str) else "control"


def offset(location: list[object]) -> int:
    return _index_int(location[2])


def length(location: list[object]) -> int:
    return _index_int(location[3])


def metadata_offset(location: list[object] | None) -> int | None:
    if location is None or location[4] is None:
        return None
    return _index_int(location[4])


def metadata_length(location: list[object] | None) -> int | None:
    if location is None or location[5] is None:
        return None
    return _index_int(location[5])


def _index_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError("Invalid read-index integer value")


def known_parent_chain(
    locations: dict[str, list[object]],
    entry_id: str,
) -> list[str]:
    result: list[str] = []
    current: str | None = entry_id
    seen: set[str] = set()
    while current is not None and current in locations and current not in seen:
        seen.add(current)
        result.append(current)
        current = parent_id(locations[current])
    return result
