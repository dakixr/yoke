"""Session index repair helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from yoke.cli.session.models import SessionIndex
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord

SESSION_INDEX_SUMMARY_VERSION = 1


def repair_index_from_session_files(
    *,
    directory: Path,
    index: SessionIndex,
    session_file_suffix: str,
    session_id_pattern: re.Pattern[str],
    load_record: Callable[[str], SessionRecord],
) -> bool:
    """Repair missing, stale, or invalid session index entries."""
    if not directory.exists():
        return False
    paths = {
        path.stem: path
        for path in directory.glob(f"*{session_file_suffix}")
        if session_id_pattern.fullmatch(path.stem)
    }
    changed = False
    for session_id in list(index.sessions):
        if session_id not in paths:
            index.sessions.pop(session_id, None)
            changed = True

    for session_id, path in sorted(paths.items()):
        existing = index.sessions.get(session_id)
        if existing is not None and _index_entry_matches_file(existing, path):
            continue
        try:
            record = load_record(session_id)
        except (OSError, ValueError):
            if existing is not None:
                index.sessions.pop(session_id, None)
                changed = True
            continue
        _upsert_index_record(
            index,
            record.model_copy(update={"id": session_id}),
            path=path,
        )
        changed = True
    return changed


def session_index_entry(record: SessionRecord, *, path: Path) -> SessionIndexEntry:
    """Build one complete lightweight session summary."""
    try:
        stat = path.stat()
        file_size = stat.st_size
        file_mtime_ns = stat.st_mtime_ns
    except OSError:
        file_size = None
        file_mtime_ns = None
    return SessionIndexEntry(
        id=record.id,
        root=record.root,
        title=record.title,
        created_at=record.created_at,
        updated_at=record.updated_at,
        pinned=record.pinned,
        archived_at=record.archived_at,
        provider_name=record.provider_name,
        model_id=record.model_id,
        reasoning_effort=record.reasoning_effort,
        leaf_id=record.leaf_id,
        entry_count=len(record.conversation_entries),
        file_size=file_size,
        file_mtime_ns=file_mtime_ns,
        summary_version=SESSION_INDEX_SUMMARY_VERSION,
    )


def _upsert_index_record(
    index: SessionIndex,
    record: SessionRecord,
    *,
    path: Path,
) -> None:
    index.sessions[record.id] = session_index_entry(record, path=path)


def _index_entry_matches_file(entry: SessionIndexEntry, path: Path) -> bool:
    if entry.summary_version != SESSION_INDEX_SUMMARY_VERSION:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return entry.file_size == stat.st_size and entry.file_mtime_ns == stat.st_mtime_ns
