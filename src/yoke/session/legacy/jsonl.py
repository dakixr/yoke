"""Decoders for the JSONL session files written before the SQLite store.

Only ``yoke db import-legacy`` reads these files.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from io import StringIO

from pydantic_core import from_json

from yoke.agent.models import ConversationEntry
from yoke.session.repository.models import SessionRecord

SESSION_JSONL_HEADER_TYPE = "yoke_session"
SESSION_JSONL_HEADER_VERSION = 2
SESSION_METADATA_EVENT = "metadata"
SESSION_ENTRY_EVENT = "entry"
SESSION_ENTRY_METADATA_EVENT = "entry_metadata"


def decode_session_record(raw_text: str) -> SessionRecord:
    """Decode the current JSONL session format."""
    return decode_session_record_lines(StringIO(raw_text))


def decode_legacy_session_record(raw_text: str) -> SessionRecord:
    """Decode session formats written before the current JSONL header."""
    stripped = raw_text.lstrip()
    if not stripped:
        raise ValueError("Session file is empty.")
    if stripped.startswith('{"type":"session_stream"'):
        return _decode_legacy_event_stream(raw_text)
    if stripped.startswith('{"type":"session_record"'):
        return SessionRecord.model_validate_json(_json_object_from_jsonl(raw_text))
    if stripped.startswith("{"):
        try:
            return SessionRecord.model_validate_json(raw_text)
        except ValueError:
            return _decode_legacy_event_stream(raw_text)
    try:
        return SessionRecord.model_validate_json(_json_object_from_jsonl(raw_text))
    except ValueError:
        return _decode_legacy_event_stream(raw_text)


def is_canonical_jsonl(raw_text: str) -> bool:
    """Return whether raw text is already the append-only JSONL format."""
    try:
        first = next(_jsonl_objects(StringIO(raw_text)), {})
    except ValueError:
        return False
    return (
        first.get("type") == SESSION_JSONL_HEADER_TYPE
        and first.get("version") == SESSION_JSONL_HEADER_VERSION
    )


def decode_session_record_lines(lines: Iterable[str]) -> SessionRecord:
    """Decode a JSONL session incrementally from text lines."""
    metadata: dict[str, object] = {}
    entries: list[ConversationEntry | None] = []
    entry_positions: dict[str, int] = {}
    saw_header = False
    for payload in _jsonl_objects(lines):
        payload_type = payload.get("type")
        if payload_type == SESSION_JSONL_HEADER_TYPE:
            saw_header = True
            continue
        if payload_type == SESSION_METADATA_EVENT:
            metadata.update(
                {key: value for key, value in payload.items() if key != "type"}
            )
            continue
        if payload_type == SESSION_ENTRY_METADATA_EVENT:
            entry_id = payload.get("entry_id")
            entry_metadata = payload.get("metadata")
            if not isinstance(entry_id, str) or not isinstance(entry_metadata, dict):
                continue
            position = entry_positions.get(entry_id)
            if position is None:
                continue
            current_entry = entries[position]
            if current_entry is None:
                continue
            entries[position] = current_entry.model_copy(
                update={"metadata": entry_metadata},
                deep=True,
            )
            continue
        if payload_type != SESSION_ENTRY_EVENT:
            continue
        entry_payload = payload.get("entry")
        if not isinstance(entry_payload, dict):
            continue
        entry = ConversationEntry.model_validate(entry_payload)
        previous_position = entry_positions.get(entry.id)
        if previous_position is not None:
            entries[previous_position] = None
        entry_positions[entry.id] = len(entries)
        entries.append(entry)
    if not saw_header:
        raise ValueError("Session JSONL event stream is missing a header.")
    if "id" not in metadata:
        raise ValueError("Session JSONL event stream is missing metadata.")
    metadata["conversation_entries"] = [entry for entry in entries if entry is not None]
    return SessionRecord.model_validate(metadata)


def _decode_legacy_event_stream(raw_text: str) -> SessionRecord:
    metadata: dict[str, object] = {}
    entries: list[ConversationEntry] = []
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for index, stripped in enumerate(lines):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if payload_type in {"session_stream", SESSION_JSONL_HEADER_TYPE}:
            continue
        if payload_type == "session_metadata" and isinstance(
            payload.get("record"), dict
        ):
            metadata.update(payload["record"])
            continue
        if payload_type == SESSION_METADATA_EVENT:
            metadata.update(
                {key: value for key, value in payload.items() if key != "type"}
            )
            continue
        if payload_type in {"conversation_entry", SESSION_ENTRY_EVENT} and isinstance(
            payload.get("entry"), dict
        ):
            entries.append(ConversationEntry.model_validate(payload["entry"]))
            continue
        if "kind" in payload and "message" in payload:
            entries.append(ConversationEntry.model_validate(payload))
            continue
        if "id" in payload:
            metadata.update(payload)
    if not entries and not metadata:
        raise ValueError("No recoverable session events found.")
    metadata["conversation_entries"] = entries
    metadata.setdefault("id", "legacy-session")
    if entries and not metadata.get("leaf_id"):
        metadata["leaf_id"] = entries[-1].id
    return SessionRecord.model_validate(metadata)


def _json_object_from_jsonl(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Session file is empty.")
    return lines[-1]


def _jsonl_objects(lines: Iterable[str]) -> Iterator[dict[str, object]]:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        payload = from_json(stripped)
        if not isinstance(payload, dict):
            continue
        yield payload
