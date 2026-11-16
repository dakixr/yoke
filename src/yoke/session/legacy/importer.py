"""Import JSONL sessions into the database and verify every imported record."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yoke.session.legacy.loader import (
    legacy_session_ids,
    load_legacy_session,
    read_legacy_index,
)
from yoke.session.repository.models import SessionIndex, SessionRecord
from yoke.session.repository.store import SessionStore

ImportStatus = Literal["imported", "skipped", "mismatch", "failed"]


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Outcome for one legacy session."""

    session_id: str
    status: ImportStatus
    detail: str | None = None
    entries: int = 0


def import_legacy_sessions(
    store: SessionStore,
    source: Path,
    session_ids: Iterable[str] | None = None,
) -> Iterator[ImportResult]:
    """Import sessions from a legacy JSONL directory, one transaction each.

    Idempotent and resumable: a session that already exists in the database
    is skipped, so a rerun continues after the last committed session. Each
    import is read back and compared with the JSONL loader's record; a
    mismatch is rolled back and reported, so a rerun retries it.
    """
    index = read_legacy_index(source)
    ids = list(session_ids) if session_ids is not None else legacy_session_ids(source)
    for session_id in ids:
        yield import_legacy_session(store, source, session_id, index=index)


def import_legacy_session(
    store: SessionStore,
    source: Path,
    session_id: str,
    *,
    index: SessionIndex | None = None,
) -> ImportResult:
    """Import and verify one legacy session."""
    try:
        record = load_legacy_session(source, session_id, index=index)
    except ValueError as exc:
        return ImportResult(session_id, "failed", str(exc))
    if record.created_at is None and not record.conversation_entries:
        return ImportResult(session_id, "failed", "Session file was not found.")
    count = len(record.conversation_entries)
    if store.exists(session_id):
        return ImportResult(session_id, "skipped", "Already in the database.", count)
    try:
        # Insert and verify in one write transaction. A mismatch rolls the
        # insert back; deleting afterwards could destroy a turn another
        # process appended between the insert and the check.
        with store.database.write() as connection:
            if not store.insert_record(record):
                return ImportResult(
                    session_id, "skipped", "Already in the database.", count
                )
            problem = verify_imported_session(store, record)
            if problem is not None:
                raise _ImportMismatch(problem)
            connection.execute(
                "UPDATE sessions SET import_hold = 1 WHERE id = ?", (session_id,)
            )
    except _ImportMismatch as mismatch:
        return ImportResult(session_id, "mismatch", str(mismatch), count)
    except Exception as exc:  # noqa: BLE001 - report and continue with the batch
        return ImportResult(session_id, "failed", f"{type(exc).__name__}: {exc}")
    return ImportResult(session_id, "imported", None, count)


class _ImportMismatch(Exception):
    """Abort the import transaction when verification fails."""


def verify_imported_session(store: SessionStore, expected: SessionRecord) -> str | None:
    """Compare the stored record with ``expected``; return a problem or None.

    Compares ``model_dump_json`` of the metadata and of every entry in order,
    which is equivalent to comparing the whole record dump without holding a
    second copy of a large session in memory.
    """
    summary = store.summary_record(expected.id)
    if summary is None:
        return "Session is missing after import."
    exclude = {"conversation_entries"}
    if summary.model_dump_json(exclude=exclude) != expected.model_dump_json(
        exclude=exclude
    ):
        return "Session metadata differs after import."
    entries = expected.conversation_entries
    count = 0
    # Close the iterator on early return so its read snapshot ends at once.
    with closing(store.iter_entries(expected.id)) as stored:
        for index, entry in enumerate(stored):
            if index >= len(entries):
                return "Database has more entries than the JSONL file."
            if entry.model_dump_json() != entries[index].model_dump_json():
                return f"Entry {index} ({entries[index].id}) differs after import."
            count += 1
    if count != len(entries):
        return f"Database has {count} entries; the JSONL file has {len(entries)}."
    return None
