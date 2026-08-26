"""Session loading and index metadata reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from yoke.cli.session.io import decode_legacy_session_record
from yoke.cli.session.io import decode_session_record_lines
from yoke.cli.session.io import is_canonical_jsonl
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.writer import append_session_metadata
from yoke.cli.session.writer import write_session_record

if TYPE_CHECKING:
    from yoke.cli.session.store import SessionStore


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

    index_entry = store._load_index().sessions.get(session_id)
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
