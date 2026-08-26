"""Session index repair helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from yoke.cli.session.io import decode_legacy_session_record
from yoke.cli.session.io import decode_session_record_lines
from yoke.cli.session.io import is_canonical_jsonl
from yoke.cli.session.models import SessionIndex
from yoke.cli.session.models import SessionIndexEntry
from yoke.cli.session.models import SessionRecord


def repair_index_from_session_files(
    *,
    directory: Path,
    index: SessionIndex,
    session_file_suffix: str,
    session_id_pattern: re.Pattern[str],
    existing_session_path: Callable[[str], Path | None],
) -> bool:
    """Add missing on-disk sessions to the index and drop missing entries."""
    if not directory.exists():
        return False
    changed = _remove_missing_index_entries(
        index=index,
        existing_session_path=existing_session_path,
    )
    return (
        _add_jsonl_sessions(
            directory=directory,
            index=index,
            session_file_suffix=session_file_suffix,
            session_id_pattern=session_id_pattern,
        )
        or changed
    )


def _remove_missing_index_entries(
    *, index: SessionIndex, existing_session_path: Callable[[str], Path | None]
) -> bool:
    changed = False
    for session_id in list(index.sessions):
        if existing_session_path(session_id) is None:
            index.sessions.pop(session_id, None)
            changed = True
    return changed


def _add_jsonl_sessions(
    *,
    directory: Path,
    index: SessionIndex,
    session_file_suffix: str,
    session_id_pattern: re.Pattern[str],
) -> bool:
    changed = False
    for path in sorted(directory.glob(f"*{session_file_suffix}")):
        session_id = path.stem
        if session_id in index.sessions or not session_id_pattern.fullmatch(session_id):
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                first_line = next((line for line in handle if line.strip()), "")
            if first_line and is_canonical_jsonl(first_line):
                with path.open(encoding="utf-8") as handle:
                    record = decode_session_record_lines(handle)
            else:
                record = decode_legacy_session_record(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            continue
        _upsert_index_record(index, record.model_copy(update={"id": session_id}))
        changed = True
    return changed


def _upsert_index_record(index: SessionIndex, record: SessionRecord) -> None:
    index.sessions[record.id] = SessionIndexEntry(
        id=record.id,
        root=record.root,
        title=record.title,
        created_at=record.created_at,
        updated_at=record.updated_at,
        pinned=record.pinned,
        archived_at=record.archived_at,
    )
