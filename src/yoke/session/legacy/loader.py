"""Read-only port of the JSONL session loader used before the SQLite store.

``load_legacy_session`` returns the record the old ``SessionStore.load``
returned for the same directory, including its schema upgrade, id repair and
reconciliation with newer metadata in ``index.json``. It never writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from yoke.session.legacy.jsonl import (
    decode_legacy_session_record,
    decode_session_record_lines,
    is_canonical_jsonl,
)
from yoke.session.repository.models import (
    SessionIndex,
    SessionIndexEntry,
    SessionRecord,
)
from yoke.session.repository.paths import (
    CURRENT_SESSION_SCHEMA_VERSION,
    SESSION_ID_PATTERN,
    validate_session_id,
)

LEGACY_SESSION_SUFFIX = ".jsonl"
LEGACY_INDEX_NAME = "index.json"


def legacy_session_ids(directory: Path) -> Iterator[str]:
    """Yield session ids of JSONL files in a legacy session directory, sorted."""
    try:
        names = sorted(path.name for path in directory.iterdir())
    except OSError:
        return
    for name in names:
        if not name.endswith(LEGACY_SESSION_SUFFIX):
            continue
        session_id = name[: -len(LEGACY_SESSION_SUFFIX)]
        if SESSION_ID_PATTERN.fullmatch(session_id):
            yield session_id


def legacy_session_path(directory: Path, session_id: str) -> Path:
    return directory / f"{validate_session_id(session_id)}{LEGACY_SESSION_SUFFIX}"


def read_legacy_index(directory: Path) -> SessionIndex:
    """Read ``index.json`` the way the old index cache did (empty on failure)."""
    try:
        raw = (directory / LEGACY_INDEX_NAME).read_text(encoding="utf-8")
    except OSError:
        return SessionIndex()
    try:
        return SessionIndex.model_validate_json(raw)
    except ValidationError:
        return SessionIndex()


def load_legacy_session(
    directory: Path,
    session_id: str,
    *,
    index: SessionIndex | None = None,
) -> SessionRecord:
    """Load one JSONL session exactly as the old store returned it."""
    path = legacy_session_path(directory, session_id)
    if not path.exists():
        return SessionRecord(id=session_id)
    resolved_index = index if index is not None else read_legacy_index(directory)
    record = _load_existing_record(
        resolved_index.sessions.get(session_id),
        session_id,
        path,
        current_schema_version=CURRENT_SESSION_SCHEMA_VERSION,
    )
    if record.version != CURRENT_SESSION_SCHEMA_VERSION:
        raise ValueError(f"Unsupported session schema version: {record.version}.")
    return record


def _load_existing_record(
    index_entry: SessionIndexEntry | None,
    session_id: str,
    path: Path,
    *,
    current_schema_version: int,
) -> SessionRecord:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = next((line for line in handle if line.strip()), "")
        if first_line and is_canonical_jsonl(first_line):
            with path.open(encoding="utf-8") as handle:
                record = decode_session_record_lines(handle)
        else:
            record = decode_legacy_session_record(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ValueError(f"Failed to load session {session_id!r}: {exc}") from exc

    if record.version not in {current_schema_version - 1, current_schema_version}:
        return record
    if record.id != session_id:
        record = record.model_copy(update={"id": session_id})
    if record.version == current_schema_version - 1:
        record = record.model_copy(update={"version": current_schema_version})
    if index_entry is None:
        return record
    changes: dict[str, object] = {}
    if _index_is_newer(index_entry, record):
        changes.update(_index_metadata_changes(index_entry, record))
    if (
        index_entry.last_user_message_at is not None
        and index_entry.last_user_message_at != record.last_user_message_at
    ):
        changes["last_user_message_at"] = index_entry.last_user_message_at
    return record.model_copy(update=changes) if changes else record


def _index_is_newer(index_entry: SessionIndexEntry, record: SessionRecord) -> bool:
    return bool(
        index_entry.updated_at
        and (record.updated_at is None or index_entry.updated_at > record.updated_at)
    )


def _index_metadata_changes(
    index_entry: SessionIndexEntry,
    record: SessionRecord,
) -> dict[str, object]:
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
