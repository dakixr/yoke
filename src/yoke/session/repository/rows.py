"""Exact encoding between session models and SQLite rows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from typing import Any, cast

from pydantic_core import from_json

from yoke.agent.message_sanitizer import sanitize_json_surrogates
from yoke.agent.models import ConversationEntry
from yoke.storage.blob_codec import decode_blob, prepare_blob
from yoke.storage.entry_transform import strip_usage_raw
from yoke.session.repository.models import (
    SESSION_INDEX_SUMMARY_VERSION,
    SessionIndexEntry,
    SessionRecord,
)

ENTRY_PAYLOAD_SELECT = (
    "SELECT e.id, e.message_sha256, b.data, e.metadata_sha256, m.data, e.metadata "
    "FROM entries e LEFT JOIN blobs b ON b.sha256 = e.message_sha256 "
    "LEFT JOIN blobs m ON m.sha256 = e.metadata_sha256 "
)


def decode_payload_row(
    connection: sqlite3.Connection,
    session_id: str,
    row: tuple,
    *,
    raw_filter: Callable[[bytes], bytes] | None = None,
) -> ConversationEntry:
    """Expand one selected payload with its entry identity in corruption errors."""
    raw, metadata = decode_payload_bytes(connection, session_id, row)
    try:
        return decode_entry(raw_filter(raw) if raw_filter else raw, metadata)
    except ValueError as exc:
        raise ValueError(f"Session {session_id} entry {row[0]}: {exc}") from exc


def decode_payload_bytes(
    connection: sqlite3.Connection, session_id: str, row: tuple
) -> tuple[bytes, str | None]:
    """Expand message and override blobs without materializing model defaults."""
    entry_id, digest, data, metadata_sha, metadata_data, metadata = row
    try:
        raw = decode_blob(connection, digest, data)
        if raw is None:
            raise ValueError(f"Missing entry blob: {digest}")
        if metadata_sha is not None:
            override = decode_blob(connection, metadata_sha, metadata_data)
            if override is None:
                raise ValueError(f"Missing metadata blob: {metadata_sha}")
            metadata = override.decode()
        return raw, metadata
    except ValueError as exc:
        raise ValueError(f"Session {session_id} entry {entry_id}: {exc}") from exc


# SessionRecord field -> sessions column. Every persisted record field is here.
RECORD_COLUMNS: dict[str, str] = {
    "version": "version",
    "origin": "origin",
    "leaf_id": "leaf_id",
    "active_skills": "active_skills",
    "skill_dirs": "skill_dirs",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "last_user_message_at": "last_user_at",
    "root": "workspace",
    "title": "title",
    "pinned": "pinned",
    "archived_at": "archived_at",
    "provider_name": "provider",
    "model_id": "model",
    "reasoning_effort": "effort",
    "context_window_tokens": "context_window_tokens",
    "context_usage": "context_usage",
}
# A row as sqlite3 returns it; column types follow the schema.
type Row = tuple[Any, ...]
_JSON_FIELDS = frozenset({"active_skills", "skill_dirs", "context_usage"})
SESSION_SELECT = (
    "SELECT id, " + ", ".join(RECORD_COLUMNS.values()) + ", tree_rev, rev, "
    "entry_count, message_bytes FROM sessions"
)


def encode_json(value: object) -> str:
    """Encode JSON exactly as the JSONL session codec did."""
    sanitized = sanitize_json_surrogates({"value": value})["value"]
    return json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False)


def encode_entry(entry: ConversationEntry) -> bytes:
    """Return the immutable entry bytes stored in ``blobs.data``."""
    payload = entry.model_dump(mode="json")
    strip_usage_raw(payload)
    return encode_json(payload).encode("utf-8")


def decode_entry(message: bytes, metadata: str | None) -> ConversationEntry:
    """Decode one stored entry, applying a later metadata replacement."""
    entry = ConversationEntry.model_validate(from_json(message))
    if metadata is not None:
        replacement = from_json(metadata)
        if isinstance(replacement, dict):
            entry.metadata = replacement
    return entry


def entry_row(session_id: str, seq: int, entry: ConversationEntry, rev: int) -> Row:
    """Return one entries row for insertion."""
    message = entry.message
    row = (
        session_id,
        seq,
        entry.id,
        entry.parent_id,
        entry.kind,
        message.role if message is not None else None,
        message.tool_call_id if message is not None else None,
        entry.created_at,
        encode_entry(entry),
        rev,
    )
    return cast(Row, sanitize_json_surrogates({"row": row})["row"])


INSERT_ENTRY = (
    "INSERT INTO entries (session_id, seq, id, parent_id, kind, role, call_id, "
    "created_at, message_sha256, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def insert_entries(
    connection: sqlite3.Connection,
    session_id: str,
    entries: Iterable[ConversationEntry],
    *,
    first_seq: int,
    rev: int,
) -> tuple[int, int]:
    """Insert entries from ``first_seq``; return (count, message bytes)."""
    totals = [0, 0]

    def rows() -> Iterator[Row]:
        for offset, entry in enumerate(entries):
            row = entry_row(session_id, first_seq + offset, entry, rev)
            totals[0] += 1
            raw = row[8]
            totals[1] += len(raw)
            prepared = prepare_blob(raw)
            digest = prepared.insert(connection)
            if not prepared.refs:
                connection.execute(
                    "INSERT OR IGNORE INTO blob_compaction VALUES (?)", (digest,)
                )
            yield (*row[:8], digest, *row[9:])

    connection.executemany(INSERT_ENTRY, rows())
    if totals[0]:
        from yoke.session.repository.provenance import classify_legacy_entries

        classify_legacy_entries(connection, session_id=session_id, first_seq=first_seq)
    return totals[0], totals[1]


def column_values(record: SessionRecord, fields: Iterable[str]) -> dict[str, object]:
    """Return sessions column values for selected record fields."""
    selected = set(fields)
    payload = sanitize_json_surrogates(record.model_dump(mode="json", include=selected))
    if "origin" in selected:
        payload["origin"] = record.origin
    values: dict[str, object] = {}
    for field, value in payload.items():
        column = RECORD_COLUMNS[field]
        if field in _JSON_FIELDS:
            values[column] = None if value is None else encode_json(value)
        elif field == "pinned":
            values[column] = int(bool(value))
        else:
            values[column] = value
    return values


def changed_fields(existing: SessionRecord, record: SessionRecord) -> list[str]:
    """Return persisted metadata fields whose JSON value differs."""
    before = existing.model_dump(mode="json", include=set(RECORD_COLUMNS))
    after = record.model_dump(mode="json", include=set(RECORD_COLUMNS))
    before["origin"] = existing.origin
    after["origin"] = record.origin
    return [field for field in RECORD_COLUMNS if before.get(field) != after.get(field)]


def metadata_payload(row: Row) -> dict[str, object]:
    """Decode a SESSION_SELECT row into SessionRecord fields."""
    payload: dict[str, object] = {"id": row[0]}
    for index, (field, _) in enumerate(RECORD_COLUMNS.items(), start=1):
        value = row[index]
        if field in _JSON_FIELDS:
            value = None if value is None else from_json(str(value))
        elif field == "pinned":
            value = bool(value)
        payload[field] = value
    return payload


_TREE_REV = len(RECORD_COLUMNS) + 1


def row_tree_rev(row: Row) -> int:
    return int(row[_TREE_REV])


def row_rev(row: Row) -> int:
    return int(row[_TREE_REV + 1])


def row_entry_count(row: Row) -> int:
    return int(row[_TREE_REV + 2])


def row_message_bytes(row: Row) -> int:
    return int(row[_TREE_REV + 3])


def record_from_row(
    row: Row,
    entries: list[ConversationEntry] | None,
) -> SessionRecord:
    """Build a record; ``entries=None`` yields a history-less summary."""
    payload = metadata_payload(row)
    payload["conversation_entries"] = entries or []
    record = SessionRecord.model_validate(payload)
    record.stamp_tree_revision(row_tree_rev(row))
    if entries is None:
        record.mark_summary()
    return record


def index_entry_from_row(row: Row) -> SessionIndexEntry:
    """Build the lightweight summary used by listings."""
    payload = metadata_payload(row)
    payload.pop("version", None)
    return SessionIndexEntry.model_validate(
        {
            **payload,
            "entry_count": row_entry_count(row),
            "file_size": row_message_bytes(row),
            "file_mtime_ns": row_rev(row),
            "summary_version": SESSION_INDEX_SUMMARY_VERSION,
        }
    )
