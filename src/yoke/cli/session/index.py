"""Session index repair helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from collections.abc import Callable
from pathlib import Path

from yoke.cli.session.models import SessionIndex
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord

SESSION_INDEX_SUMMARY_VERSION = 3


@dataclass(frozen=True, slots=True)
class SessionFileSnapshot:
    """One session path plus metadata captured during directory enumeration."""

    path: Path
    size: int | None
    mtime_ns: int | None


def repair_index_from_session_files(
    *,
    directory: Path,
    index: SessionIndex,
    session_file_suffix: str,
    session_id_pattern: re.Pattern[str],
    load_summary: Callable[[str], tuple[SessionRecord, int]],
) -> bool:
    """Repair missing, stale, or invalid session index entries."""
    paths = _session_file_snapshots(
        directory,
        session_file_suffix=session_file_suffix,
        session_id_pattern=session_id_pattern,
    )
    if paths is None:
        return False
    changed = False
    for session_id in list(index.sessions):
        if session_id not in paths:
            index.sessions.pop(session_id, None)
            changed = True

    for session_id, snapshot in sorted(paths.items()):
        existing = index.sessions.get(session_id)
        if existing is not None and _index_entry_matches_file(existing, snapshot):
            continue
        try:
            record, entry_count = load_summary(session_id)
        except OSError:
            # A transient read failure does not prove an existing session is gone.
            # Keep its old signature so the next repair retries the summary.
            continue
        except ValueError:
            if existing is not None:
                index.sessions.pop(session_id, None)
                changed = True
            continue
        _upsert_index_record(
            index,
            record.model_copy(update={"id": session_id}),
            snapshot=snapshot,
            entry_count=entry_count,
        )
        changed = True
    return changed


def session_index_entry(
    record: SessionRecord,
    *,
    path: Path,
    entry_count: int | None = None,
    file_signature: tuple[int, int] | None = None,
) -> SessionIndexEntry:
    """Build one complete lightweight session summary."""
    if file_signature is None:
        try:
            stat = path.stat()
            file_size = stat.st_size
            file_mtime_ns = stat.st_mtime_ns
        except OSError:
            file_size = None
            file_mtime_ns = None
    else:
        file_size, file_mtime_ns = file_signature
    return SessionIndexEntry(
        id=record.id,
        root=record.root,
        title=record.title,
        created_at=record.created_at,
        updated_at=record.updated_at,
        last_user_message_at=record.last_user_message_at,
        pinned=record.pinned,
        archived_at=record.archived_at,
        provider_name=record.provider_name,
        model_id=record.model_id,
        reasoning_effort=record.reasoning_effort,
        context_window_tokens=record.context_window_tokens,
        context_usage=(dict(record.context_usage) if record.context_usage else None),
        leaf_id=record.leaf_id,
        active_skills=[skill.model_copy(deep=True) for skill in record.active_skills],
        skill_dirs=list(record.skill_dirs),
        entry_count=(
            len(record.conversation_entries) if entry_count is None else entry_count
        ),
        file_size=file_size,
        file_mtime_ns=file_mtime_ns,
        summary_version=SESSION_INDEX_SUMMARY_VERSION,
    )


def _upsert_index_record(
    index: SessionIndex,
    record: SessionRecord,
    *,
    snapshot: SessionFileSnapshot,
    entry_count: int | None = None,
) -> None:
    file_signature = (
        (snapshot.size, snapshot.mtime_ns)
        if snapshot.size is not None and snapshot.mtime_ns is not None
        else None
    )
    index.sessions[record.id] = session_index_entry(
        record,
        path=snapshot.path,
        entry_count=entry_count,
        file_signature=file_signature,
    )


def _index_entry_matches_file(
    entry: SessionIndexEntry,
    snapshot: SessionFileSnapshot,
) -> bool:
    if entry.summary_version != SESSION_INDEX_SUMMARY_VERSION:
        return False
    if snapshot.size is None or snapshot.mtime_ns is None:
        return False
    return entry.file_size == snapshot.size and entry.file_mtime_ns == snapshot.mtime_ns


def _session_file_snapshots(
    directory: Path,
    *,
    session_file_suffix: str,
    session_id_pattern: re.Pattern[str],
) -> dict[str, SessionFileSnapshot] | None:
    normalized_suffix = os.path.normcase(session_file_suffix)
    snapshots: dict[str, SessionFileSnapshot] = {}
    try:
        with os.scandir(directory) as entries:
            for item in entries:
                normalized_name = os.path.normcase(item.name)
                if not normalized_name.endswith(normalized_suffix):
                    continue
                session_id = item.name[: -len(session_file_suffix)]
                if not session_id_pattern.fullmatch(session_id):
                    continue
                try:
                    stat = item.stat()
                except OSError:
                    size = None
                    mtime_ns = None
                else:
                    size = stat.st_size
                    mtime_ns = stat.st_mtime_ns
                snapshots[session_id] = SessionFileSnapshot(
                    path=Path(item.path),
                    size=size,
                    mtime_ns=mtime_ns,
                )
    except OSError:
        return None
    return snapshots


def session_file_ids(
    directory: Path,
    *,
    session_file_suffix: str,
    session_id_pattern: re.Pattern[str],
) -> set[str] | None:
    """Return platform-normalized IDs from one directory enumeration."""
    normalized_suffix = os.path.normcase(session_file_suffix)
    session_ids: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            for item in entries:
                normalized_name = os.path.normcase(item.name)
                if not normalized_name.endswith(normalized_suffix):
                    continue
                session_id = item.name[: -len(session_file_suffix)]
                if session_id_pattern.fullmatch(session_id):
                    session_ids.add(os.path.normcase(session_id))
    except OSError:
        return None
    return session_ids
