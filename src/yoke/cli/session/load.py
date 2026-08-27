"""Session loading and index metadata reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from pydantic_core import from_json

from yoke.cli.session.io import SESSION_ENTRY_EVENT
from yoke.cli.session.io import SESSION_ENTRY_METADATA_EVENT
from yoke.cli.session.io import SESSION_JSONL_HEADER_TYPE
from yoke.cli.session.io import SESSION_JSONL_HEADER_VERSION
from yoke.cli.session.io import SESSION_METADATA_EVENT
from yoke.cli.session.io import decode_legacy_session_record
from yoke.cli.session.io import decode_session_record_lines
from yoke.cli.session.io import is_canonical_jsonl
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.writer import append_session_metadata
from yoke.cli.session.writer import write_session_record

if TYPE_CHECKING:
    from yoke.cli.session.store import SessionStore


def scan_canonical_session_summary(
    path: Path,
    session_id: str,
) -> tuple[SessionRecord, int] | None:
    """Read canonical session metadata and topology without decoding messages."""
    metadata: dict[str, object] = {}
    entry_ids: set[str] = set()
    saw_header = False
    try:
        with path.open("rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if line.startswith(b'{"type":"entry",'):
                    entry_id = _canonical_entry_id(line)
                    if entry_id is None:
                        return None
                    entry_ids.add(entry_id)
                    continue
                try:
                    payload = from_json(line)
                except ValueError:
                    return None
                if not isinstance(payload, dict):
                    return None
                payload_type = payload.get("type")
                if payload_type == SESSION_JSONL_HEADER_TYPE:
                    if payload.get("version") != SESSION_JSONL_HEADER_VERSION:
                        return None
                    saw_header = True
                    continue
                if payload_type == SESSION_METADATA_EVENT:
                    metadata.update(
                        {key: value for key, value in payload.items() if key != "type"}
                    )
                    continue
                if payload_type == SESSION_ENTRY_METADATA_EVENT:
                    continue
                if payload_type == SESSION_ENTRY_EVENT:
                    # Non-canonical field ordering. The normal path above
                    # intentionally avoids decoding the historical body.
                    raw_entry = payload.get("entry")
                    if not isinstance(raw_entry, dict):
                        return None
                    entry_id = raw_entry.get("id")
                    if not isinstance(entry_id, str):
                        return None
                    entry_ids.add(entry_id)
                    continue
                return None
    except OSError:
        return None
    if not saw_header or not metadata:
        return None
    metadata["id"] = session_id
    metadata["conversation_entries"] = []
    try:
        return SessionRecord.model_validate(metadata), len(entry_ids)
    except ValidationError:
        return None


def reconcile_index_owned_metadata(
    record: SessionRecord,
    index_entry: SessionIndexEntry | None,
) -> SessionRecord:
    """Apply newer index-owned title/pin/archive metadata to a summary record."""
    if index_entry is None or not _index_is_newer(index_entry, record):
        return record
    changes = _index_metadata_changes(index_entry, record)
    return record.model_copy(update=changes) if changes else record


def _canonical_entry_id(line: bytes) -> str | None:
    marker = b',"id":'
    parent_marker = b',"parent_id":'
    parent_at = line.rfind(parent_marker)
    id_at = line.rfind(marker, 0, parent_at if parent_at >= 0 else len(line))
    if id_at < 0 or parent_at < 0 or id_at >= parent_at:
        return None
    raw = line[id_at + len(marker) : parent_at]
    if len(raw) >= 2 and raw[0] == 34 and raw[-1] == 34 and b"\\" not in raw:
        try:
            return raw[1:-1].decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        value = from_json(raw)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def load_existing_record(
    store: SessionStore,
    session_id: str,
    path: Path,
    *,
    current_schema_version: int,
) -> SessionRecord:
    """Load a record and preserve newer index title or pin edits in JSONL."""
    needs_rewrite = False
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = next((line for line in handle if line.strip()), "")
        if first_line and is_canonical_jsonl(first_line):
            with path.open(encoding="utf-8") as handle:
                record = decode_session_record_lines(handle)
        else:
            record = decode_legacy_session_record(path.read_text(encoding="utf-8"))
            needs_rewrite = True
    except (OSError, ValidationError, ValueError) as exc:
        raise ValueError(f"Failed to load session {session_id!r}: {exc}") from exc

    if record.version not in {current_schema_version - 1, current_schema_version}:
        return record
    if record.id != session_id:
        record = record.model_copy(update={"id": session_id})
        needs_rewrite = True
    if record.version == current_schema_version - 1:
        record = record.model_copy(update={"version": current_schema_version})
        needs_rewrite = True

    index_entry = store.index_entry(session_id)
    if index_entry is None or not _index_is_newer(index_entry, record):
        if needs_rewrite:
            write_session_record(record, path=path)
            store._update_index(record)
        return record
    changes = _index_metadata_changes(index_entry, record)
    if not changes:
        if needs_rewrite:
            write_session_record(record, path=path)
            store._update_index(record)
        return record
    record = record.model_copy(update=changes)
    if needs_rewrite:
        write_session_record(record, path=path)
        store._update_index(record)
    else:
        append_session_metadata(path, changes)
    return record


def _index_is_newer(index_entry: SessionIndexEntry, record: SessionRecord) -> bool:
    """Compare ISO-8601 timestamps without trusting absent values."""
    return bool(
        index_entry.updated_at
        and (record.updated_at is None or index_entry.updated_at > record.updated_at)
    )


def _index_metadata_changes(
    index_entry: SessionIndexEntry,
    record: SessionRecord,
) -> dict[str, object]:
    """Return index-owned metadata that differs from the session stream."""
    candidates = {
        "root": index_entry.root,
        "title": index_entry.title,
        "pinned": index_entry.pinned,
        "archived_at": index_entry.archived_at,
        "updated_at": index_entry.updated_at,
    }
    return {
        key: value for key, value in candidates.items() if getattr(record, key) != value
    }
