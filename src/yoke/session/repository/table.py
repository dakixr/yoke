"""Transactional session, entry and topology rows in the Yoke database."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from yoke.agent.models import ConversationEntry
from yoke.session.repository.models import (
    SessionIndexEntry,
    SessionRecord,
    SessionTreeConflict,
)
from yoke.session.repository.rows import (
    RECORD_COLUMNS,
    SESSION_SELECT,
    Row,
    changed_fields,
    column_values,
    decode_entry,
    encode_json,
    index_entry_from_row,
    insert_entries,
    record_from_row,
    row_message_bytes,
    row_rev,
)
from yoke.session.repository.utils import parse_timestamp
from yoke.storage import Database
from yoke.storage.blob_codec import prepare_blob
from yoke.storage.entry_transform import strip_usage_raw

_READ_CHUNK = 500


@dataclass(frozen=True, slots=True)
class EntryTopology:
    """Entry identities in stored order, without message payloads."""

    signature: tuple[int, int]
    leaf_id: str | None
    rows: list[tuple[str, str | None, str]]


@dataclass(frozen=True, slots=True)
class _State:
    rev: int
    tree_rev: int
    entry_seq: int
    entry_count: int
    message_bytes: int


class SessionTable:
    """Own every SQL statement over sessions and entries."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # Reads -----------------------------------------------------------------

    def load(self, session_id: str) -> SessionRecord | None:
        with self.database.read() as connection:
            row = _session_row(connection, session_id)
            if row is None:
                return None
            entries = [
                decode_entry(message, metadata)
                for message, metadata in connection.execute(
                    "SELECT yoke_blob_data(blobs.sha256, blobs.data), COALESCE(CAST(yoke_blob_data(entries.metadata_sha256, (SELECT data FROM blobs WHERE sha256 = entries.metadata_sha256)) AS TEXT), entries.metadata) FROM entries JOIN blobs ON blobs.sha256 = entries.message_sha256 "
                    "WHERE session_id = ? ORDER BY seq",
                    (session_id,),
                )
            ]
        return record_from_row(row, entries)

    def summary(self, session_id: str) -> SessionRecord | None:
        with self.database.read() as connection:
            row = _session_row(connection, session_id)
        return None if row is None else record_from_row(row, None)

    def index_entry(self, session_id: str) -> SessionIndexEntry | None:
        with self.database.read() as connection:
            row = _session_row(connection, session_id)
        return None if row is None else index_entry_from_row(row)

    def index_entries(self, root: str | None) -> list[SessionIndexEntry]:
        with self.database.read() as connection:
            if root is None:
                rows = connection.execute(SESSION_SELECT).fetchall()
            else:
                rows = connection.execute(
                    SESSION_SELECT + " WHERE workspace = ?", (root,)
                ).fetchall()
        return [index_entry_from_row(row) for row in rows]

    def exists(self, session_id: str) -> bool:
        with self.database.read() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                is not None
            )

    def signature(self, session_id: str) -> tuple[int, int] | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT message_bytes, rev FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def topology(self, session_id: str) -> EntryTopology | None:
        with self.database.read() as connection:
            row = _session_row(connection, session_id)
            if row is None:
                return None
            rows = connection.execute(
                "SELECT id, parent_id, kind FROM entries "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        leaf_id = record_from_row(row, None).leaf_id
        return EntryTopology(
            signature=(row_message_bytes(row), row_rev(row)),
            leaf_id=leaf_id,
            rows=[(str(item[0]), item[1], str(item[2])) for item in rows],
        )

    def read_entries(
        self,
        session_id: str,
        entry_ids: Sequence[str],
        *,
        raw_filter: Callable[[bytes], bytes] | None = None,
    ) -> list[ConversationEntry] | None:
        """Read entries by id in the requested order; None if any is missing.

        ``raw_filter`` may rewrite stored bytes before decoding, for example
        to drop inline image data a caller does not need.
        """
        found: dict[str, ConversationEntry] = {}
        unique = list(dict.fromkeys(entry_ids))
        with self.database.read() as connection:
            for start in range(0, len(unique), _READ_CHUNK):
                chunk = unique[start : start + _READ_CHUNK]
                marks = ", ".join("?" for _ in chunk)
                for entry_id, message, metadata in connection.execute(
                    "SELECT entries.id, yoke_blob_data(blobs.sha256, blobs.data), COALESCE(CAST(yoke_blob_data(entries.metadata_sha256, (SELECT data FROM blobs WHERE sha256 = entries.metadata_sha256)) AS TEXT), entries.metadata) FROM entries JOIN blobs ON blobs.sha256 = entries.message_sha256 "
                    f"WHERE session_id = ? AND id IN ({marks})",
                    (session_id, *chunk),
                ):
                    raw = raw_filter(message) if raw_filter is not None else message
                    found[entry_id] = decode_entry(raw, metadata)
        if len(found) != len(unique):
            return None
        result: list[ConversationEntry] = []
        seen: set[str] = set()
        for entry_id in entry_ids:
            entry = found[entry_id]
            result.append(entry.model_copy(deep=True) if entry_id in seen else entry)
            seen.add(entry_id)
        return result

    def iter_entries(self, session_id: str) -> Generator[ConversationEntry]:
        """Decode stored entries one at a time from one read snapshot."""
        with self.database.stream_read() as connection:
            for message, metadata in connection.execute(
                "SELECT yoke_blob_data(blobs.sha256, blobs.data), COALESCE(CAST(yoke_blob_data(entries.metadata_sha256, (SELECT data FROM blobs WHERE sha256 = entries.metadata_sha256)) AS TEXT), entries.metadata) FROM entries JOIN blobs ON blobs.sha256 = entries.message_sha256 "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ):
                yield decode_entry(message, metadata)

    # Writes ----------------------------------------------------------------

    def insert_record(self, record: SessionRecord) -> bool:
        """Insert a complete record unchanged; False if the id is taken."""
        with self.database.write() as connection:
            if _state(connection, record.id) is not None:
                return False
            _insert_record(connection, record, record.conversation_entries)
            return True

    def delete(self, session_id: str) -> bool:
        """Delete a session and its entries, events and inputs."""
        with self.database.write() as connection:
            if _state(connection, session_id) is None:
                return False
            delete_session_rows(connection, session_id)
            return True

    def write_record(
        self,
        record: SessionRecord,
        *,
        existing: SessionRecord,
        appended: Sequence[ConversationEntry] | None,
    ) -> int:
        """Persist a save: append when proven, otherwise rewrite the entries.

        Only metadata fields that differ from ``existing`` are written, so a
        concurrent title or pin edit by another process is not overwritten.
        """
        with self.database.write() as connection:
            state = _state(connection, record.id)
            if state is None:
                return _insert_record(
                    connection,
                    record,
                    [*record.conversation_entries, *(appended or ())],
                )
            rev = state.rev + 1
            new: Sequence[ConversationEntry] | None = appended
            if new is None and _extends(
                existing.conversation_entries, record.conversation_entries
            ):
                new = record.conversation_entries[len(existing.conversation_entries) :]
            updates: dict[str, object] = {}
            if new is None:
                connection.execute(
                    "DELETE FROM entries WHERE session_id = ?", (record.id,)
                )
                count, size = insert_entries(
                    connection,
                    record.id,
                    record.conversation_entries,
                    first_seq=state.entry_seq + 1,
                    rev=rev,
                )
                updates.update(entry_count=count, message_bytes=size, rewrite_rev=rev)
                tree_changed = True
            else:
                count, size = insert_entries(
                    connection, record.id, new, first_seq=state.entry_seq + 1, rev=rev
                )
                updates.update(
                    entry_count=state.entry_count + count,
                    message_bytes=state.message_bytes + size,
                )
                tree_changed = count > 0
            fields = changed_fields(existing, record)
            tree_changed = tree_changed or "leaf_id" in fields
            updates.update(column_values(record, fields))
            updates.update(entry_seq=state.entry_seq + count, rev=rev, import_hold=0)
            tree_rev = state.tree_rev + int(tree_changed)
            updates["tree_rev"] = tree_rev
            _update_session(connection, record.id, updates)
            return tree_rev

    def update_metadata(
        self,
        record: SessionRecord,
        fields: Iterable[str],
        *,
        insert_if_missing: bool = False,
    ) -> int | None:
        """Write selected metadata fields; insert the whole record if absent."""
        selected = list(fields)
        with self.database.write() as connection:
            state = _state(connection, record.id)
            if state is None:
                if not insert_if_missing:
                    return None
                return _insert_record(connection, record, record.conversation_entries)
            updates = column_values(record, selected)
            tree_rev = state.tree_rev + int("leaf_id" in selected)
            updates.update(rev=state.rev + 1, tree_rev=tree_rev)
            _update_session(connection, record.id, updates)
            return tree_rev

    def append_tree(
        self,
        record: SessionRecord,
        fields: Iterable[str],
        entries: Sequence[ConversationEntry],
        *,
        expected_tree_rev: int | None,
        require_existing: bool,
    ) -> int:
        """Append proven entries and write tree metadata, compare-and-swap."""
        with self.database.write() as connection:
            state = _state(connection, record.id)
            if state is None:
                if require_existing:
                    raise ValueError(f"Session not found: {record.id}")
                return _insert_record(
                    connection, record, [*record.conversation_entries, *entries]
                )
            _require_tree_rev(record.id, state, expected_tree_rev)
            rev = state.rev + 1
            count, size = insert_entries(
                connection, record.id, entries, first_seq=state.entry_seq + 1, rev=rev
            )
            updates = column_values(record, fields)
            updates.update(
                entry_seq=state.entry_seq + count,
                entry_count=state.entry_count + count,
                message_bytes=state.message_bytes + size,
                rev=rev,
                tree_rev=state.tree_rev + 1,
                import_hold=0,
            )
            _update_session(connection, record.id, updates)
            return state.tree_rev + 1

    def replace_entry_metadata(
        self,
        record: SessionRecord,
        entry: ConversationEntry,
        fields: Iterable[str],
        *,
        expected_tree_rev: int | None,
        require_existing: bool,
    ) -> int:
        """Replace one entry's metadata, compare-and-swap on the tree revision."""
        with self.database.write() as connection:
            state = _state(connection, record.id)
            if state is None:
                if require_existing:
                    raise ValueError(f"Session not found: {record.id}")
                return _insert_record(connection, record, record.conversation_entries)
            _require_tree_rev(record.id, state, expected_tree_rev)
            rev = state.rev + 1
            payload = deepcopy(entry.metadata)
            strip_usage_raw(payload)
            prepared = prepare_blob(encode_json(payload).encode())
            digest = prepared.insert(connection)
            if not prepared.refs:
                connection.execute(
                    "INSERT OR IGNORE INTO blob_compaction VALUES (?)", (digest,)
                )
            changed = connection.execute(
                "UPDATE entries SET metadata = NULL, metadata_sha256 = ?, rev = ? "
                "WHERE session_id = ? AND id = ?",
                (digest, rev, record.id, entry.id),
            ).rowcount
            if changed != 1:
                raise ValueError("Entry is not in this session.")
            updates = column_values(record, fields)
            updates.update(rev=rev, tree_rev=state.tree_rev + 1, import_hold=0)
            _update_session(connection, record.id, updates)
            return state.tree_rev + 1

    def fork(self, source_id: str, record: SessionRecord) -> bool:
        """Copy a session's entries under ``record``'s id and metadata in SQL."""
        with self.database.write() as connection:
            if _state(connection, record.id) is not None:
                raise ValueError(f"Session already exists: {record.id}")
            source = _state(connection, source_id)
            if source is None:
                return False
            columns = column_values(record, RECORD_COLUMNS)
            columns.update(
                id=record.id,
                rev=1,
                tree_rev=1,
                entry_seq=source.entry_seq,
                entry_count=source.entry_count,
                message_bytes=source.message_bytes,
            )
            _insert_session(connection, columns)
            connection.execute(
                "INSERT INTO entries (session_id, seq, id, parent_id, kind, role, "
                "call_id, created_at, message_sha256, metadata, metadata_sha256, rev) "
                "SELECT ?, seq, id, parent_id, kind, role, call_id, created_at, "
                "message_sha256, metadata, metadata_sha256, 1 FROM entries WHERE session_id = ?",
                (record.id, source_id),
            )
            return True

    def prune(self, retention_days: int) -> list[str]:
        """Delete unpinned sessions idle for longer than the retention window."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        expired = []
        # Recheck pins and activity in the same transaction that deletes rows.
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT id, updated_at, created_at FROM sessions "
                "WHERE pinned = 0 AND import_hold = 0"
            ).fetchall()
            for session_id, updated_at, created_at in rows:
                activity = parse_timestamp(updated_at) or parse_timestamp(created_at)
                if activity is None:
                    continue
                if activity.tzinfo is None:
                    activity = activity.replace(tzinfo=UTC)
                if activity < cutoff:
                    delete_session_rows(connection, session_id)
                    expired.append(session_id)
        return expired


def delete_session_rows(connection: sqlite3.Connection, session_id: str) -> None:
    """Delete a session and every row keyed by it."""
    for table in ("events", "inputs", "session_queues"):
        connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
    connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def _session_row(connection: sqlite3.Connection, session_id: str) -> Row | None:
    return connection.execute(
        SESSION_SELECT + " WHERE id = ?", (session_id,)
    ).fetchone()


def _state(connection: sqlite3.Connection, session_id: str) -> _State | None:
    row = connection.execute(
        "SELECT rev, tree_rev, entry_seq, entry_count, message_bytes "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return None if row is None else _State(*(int(value) for value in row))


def _require_tree_rev(session_id: str, state: _State, expected: int | None) -> None:
    if expected is not None and state.tree_rev != expected:
        raise SessionTreeConflict(session_id, expected, state.tree_rev)


def _insert_record(
    connection: sqlite3.Connection,
    record: SessionRecord,
    entries: Sequence[ConversationEntry],
) -> int:
    tree_rev = 1 if entries or record.leaf_id is not None else 0
    columns = column_values(record, RECORD_COLUMNS)
    columns.update(id=record.id, rev=1, tree_rev=tree_rev)
    _insert_session(connection, columns)
    count, size = insert_entries(connection, record.id, entries, first_seq=1, rev=1)
    _update_session(
        connection,
        record.id,
        {"entry_seq": count, "entry_count": count, "message_bytes": size},
    )
    return tree_rev


def _insert_session(connection: sqlite3.Connection, columns: dict[str, object]) -> None:
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO sessions ({names}) VALUES ({marks})", tuple(columns.values())
    )


def _update_session(
    connection: sqlite3.Connection, session_id: str, updates: dict[str, object]
) -> None:
    if not updates:
        return
    assignments = ", ".join(f"{name} = ?" for name in updates)
    connection.execute(
        f"UPDATE sessions SET {assignments} WHERE id = ?",
        (*updates.values(), session_id),
    )


def _extends(
    existing: Sequence[ConversationEntry], new: Sequence[ConversationEntry]
) -> bool:
    if len(new) < len(existing):
        return False
    return all(entry == new[index] for index, entry in enumerate(existing))
