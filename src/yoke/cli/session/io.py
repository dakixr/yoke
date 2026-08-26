"""Session file serialization helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from io import StringIO

from pydantic_core import from_json

from yoke.agent.message_sanitizer import sanitize_json_surrogates
from yoke.agent.models import ConversationEntry
from yoke.cli.session.models import SessionRecord

SESSION_JSONL_HEADER_TYPE = "yoke_session"
SESSION_JSONL_HEADER_VERSION = 2
SESSION_METADATA_EVENT = "metadata"
SESSION_ENTRY_EVENT = "entry"
SESSION_ENTRY_METADATA_EVENT = "entry_metadata"


def decode_session_record(raw_text: str) -> SessionRecord:
    """Decode the current JSONL session format."""
    return decode_session_record_lines(StringIO(raw_text))


def decode_session_record_lines(lines: Iterable[str]) -> SessionRecord:
    """Decode a JSONL session incrementally from text lines."""
    return _decode_event_stream(lines)


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


def record_jsonl(record: SessionRecord) -> str:
    """Encode a session record as a canonical JSONL event stream."""
    return "".join(record_jsonl_lines(record))


def record_jsonl_lines(record: SessionRecord) -> list[str]:
    """Return canonical JSONL lines for a complete session record."""
    return [
        _jsonl_line(
            {
                "type": SESSION_JSONL_HEADER_TYPE,
                "version": SESSION_JSONL_HEADER_VERSION,
            }
        ),
        _jsonl_line(_metadata_event(record)),
        *(_jsonl_line(_entry_event(entry)) for entry in record.conversation_entries),
    ]


def append_jsonl_lines(
    existing_record: SessionRecord,
    new_record: SessionRecord,
) -> list[str] | None:
    """Return append-only JSONL lines, or None if a rewrite is required."""
    if not _entries_are_append_only(
        existing_record.conversation_entries,
        new_record.conversation_entries,
    ):
        return None
    existing_count = len(existing_record.conversation_entries)
    metadata_delta = _metadata_delta_event(existing_record, new_record)
    lines = [_jsonl_line(metadata_delta)] if len(metadata_delta) > 1 else []
    lines.extend(
        _jsonl_line(_entry_event(entry))
        for entry in new_record.conversation_entries[existing_count:]
    )
    return lines


def trusted_append_jsonl_lines(
    existing_record: SessionRecord,
    new_record: SessionRecord,
    appended_entries: Iterable[ConversationEntry],
) -> list[str]:
    """Encode an append delta whose topology was already proven by its owner."""
    metadata_delta = _metadata_delta_event(existing_record, new_record)
    lines = [_jsonl_line(metadata_delta)] if len(metadata_delta) > 1 else []
    lines.extend(_jsonl_line(_entry_event(entry)) for entry in appended_entries)
    return lines


def metadata_delta_jsonl_line(changes: dict[str, object]) -> str:
    """Encode a small metadata update as one JSONL event."""
    if "type" in changes or "conversation_entries" in changes:
        raise ValueError("Metadata delta contains a reserved session field.")
    return _jsonl_line({"type": SESSION_METADATA_EVENT, **changes})


def entry_metadata_jsonl_line(
    entry_id: str,
    metadata: dict[str, object],
) -> str:
    """Encode an in-place metadata replacement for one persisted entry."""
    return _jsonl_line(
        {
            "type": SESSION_ENTRY_METADATA_EVENT,
            "entry_id": entry_id,
            "metadata": metadata,
        }
    )


def is_canonical_jsonl(raw_text: str) -> bool:
    """Return whether raw text is already the append-only JSONL format."""
    try:
        first = _first_jsonl_object(raw_text)
    except ValueError:
        return False
    return (
        first.get("type") == SESSION_JSONL_HEADER_TYPE
        and first.get("version") == SESSION_JSONL_HEADER_VERSION
    )


def _decode_event_stream(lines: Iterable[str]) -> SessionRecord:
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


def _metadata_event(record: SessionRecord) -> dict[str, object]:
    return {"type": SESSION_METADATA_EVENT, **_metadata_payload(record)}


def _metadata_delta_event(
    existing_record: SessionRecord,
    new_record: SessionRecord,
) -> dict[str, object]:
    existing = _metadata_payload(existing_record)
    current = _metadata_payload(new_record)
    return {
        "type": SESSION_METADATA_EVENT,
        **{key: value for key, value in current.items() if existing.get(key) != value},
    }


def _metadata_payload(record: SessionRecord) -> dict[str, object]:
    return record.model_dump(
        mode="json",
        exclude={"conversation_entries"},
    )


def _entry_event(entry: ConversationEntry) -> dict[str, object]:
    return {
        "type": SESSION_ENTRY_EVENT,
        "entry": entry.model_dump(mode="json"),
    }


def _entries_are_append_only(
    existing_entries: list[ConversationEntry],
    new_entries: list[ConversationEntry],
) -> bool:
    if len(new_entries) < len(existing_entries):
        return False
    return all(
        existing_entry == new_entries[index]
        for index, existing_entry in enumerate(existing_entries)
    )


def _jsonl_line(payload: dict[str, object]) -> str:
    sanitized_payload = sanitize_json_surrogates(payload)
    return (
        json.dumps(
            sanitized_payload,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def _first_jsonl_object(raw_text: str) -> dict[str, object]:
    for line in StringIO(raw_text):
        stripped = line.strip()
        if not stripped:
            continue
        payload = from_json(stripped)
        if isinstance(payload, dict):
            return payload
    raise ValueError("Session file is empty.")


def _json_object_from_jsonl(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Session file is empty.")
    return lines[-1]


def _jsonl_objects(lines: Iterable[str]) -> Iterable[dict[str, object]]:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        payload = from_json(stripped)
        if not isinstance(payload, dict):
            continue
        yield payload
