"""Atomic and append-only writers for CLI session files."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from yoke.agent.models import ConversationEntry
from yoke.cli.session.io import append_jsonl_lines
from yoke.cli.session.io import decode_session_record_lines
from yoke.cli.session.io import entry_metadata_jsonl_line
from yoke.cli.session.io import is_canonical_jsonl
from yoke.cli.session.io import metadata_delta_jsonl_line
from yoke.cli.session.io import record_jsonl
from yoke.cli.session.io import trusted_append_jsonl_lines
from yoke.cli.session.models import SessionRecord


def write_session_record(
    record: SessionRecord,
    *,
    path: Path,
    existing_record: SessionRecord | None = None,
    trusted_append_entries: tuple[ConversationEntry, ...] | None = None,
) -> Path:
    """Write a session, appending when the existing file allows it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _append_record_if_possible(
        record,
        path=path,
        existing_record=existing_record,
        trusted_append_entries=trusted_append_entries,
    ):
        return path
    rewrite_record = record
    if trusted_append_entries:
        rewrite_record = record.model_copy(
            update={
                "conversation_entries": [
                    *record.conversation_entries,
                    *trusted_append_entries,
                ]
            }
        )
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(record_jsonl(rewrite_record), encoding="utf-8")
    try:
        _replace_with_retries(tmp_path, path)
    finally:
        _remove_path_if_exists(tmp_path)
    return path


def append_session_metadata(path: Path, changes: dict[str, object]) -> None:
    """Append one metadata delta without reading the session file."""
    if not changes:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(metadata_delta_jsonl_line(changes))


def append_session_entry_metadata(
    path: Path,
    *,
    entry_id: str,
    entry_metadata: dict[str, object],
    session_changes: dict[str, object],
) -> None:
    """Append session and entry metadata changes in one file operation."""
    with path.open("a", encoding="utf-8") as handle:
        if session_changes:
            handle.write(metadata_delta_jsonl_line(session_changes))
        handle.write(entry_metadata_jsonl_line(entry_id, entry_metadata))


def _append_record_if_possible(
    record: SessionRecord,
    *,
    path: Path,
    existing_record: SessionRecord | None,
    trusted_append_entries: tuple[ConversationEntry, ...] | None,
) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = handle.readline()
        if not is_canonical_jsonl(first_line):
            return False
        resolved_existing = existing_record
        if resolved_existing is None:
            with path.open(encoding="utf-8") as handle:
                resolved_existing = decode_session_record_lines(handle)
        lines = (
            trusted_append_jsonl_lines(
                resolved_existing,
                record,
                trusted_append_entries,
            )
            if trusted_append_entries is not None
            else append_jsonl_lines(resolved_existing, record)
        )
        if lines is None:
            return False
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(lines)
    except (OSError, ValidationError, ValueError):
        return False
    return True


def _replace_with_retries(tmp_path: Path, path: Path) -> None:
    for attempt in range(4):
        try:
            tmp_path.replace(path)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


def _remove_path_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
