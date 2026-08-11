"""Session loading and index metadata reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from yoke.cli.session.io import decode_session_record_lines
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord
from yoke.cli.session.writer import append_session_metadata

if TYPE_CHECKING:
    from yoke.cli.session.store import SessionStore


def load_existing_record(
    store: SessionStore,
    session_id: str,
    path: Path,
) -> SessionRecord:
    """Load a record and preserve newer index title or pin edits in JSONL."""
    try:
        with path.open(encoding="utf-8") as handle:
            record = decode_session_record_lines(handle)
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Failed to load session {session_id!r}: {exc}") from exc

    index_entry = store._load_index().sessions.get(session_id)
    if index_entry is None or not _index_is_newer(index_entry, record):
        return record
    changes = _index_metadata_changes(index_entry, record)
    if not changes:
        return record
    append_session_metadata(path, changes)
    return record.model_copy(update=changes)


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
        "updated_at": index_entry.updated_at,
    }
    return {
        key: value for key, value in candidates.items() if getattr(record, key) != value
    }
