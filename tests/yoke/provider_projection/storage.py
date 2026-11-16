"""Replay pre-codec SQLite entry bytes through the new background compactor."""

from __future__ import annotations

from yoke.session import SessionStore
from yoke.storage.compactor import compact_database


def compact_legacy_snapshot(store: SessionStore) -> None:
    """Restore inline representation, compact, and preserve public activity times.

    Oracle image fixtures already fit both limits and are smaller than JPEG,
    so image re-encoding is fixed at identity without replacing goldens.
    """
    database = store.database
    with database.write() as connection:
        blobs = connection.execute(
            "SELECT sha256, yoke_blob_data(sha256, data) FROM blobs"
        ).fetchall()
        metadata = connection.execute(
            "SELECT e.session_id, e.seq, yoke_blob_data(b.sha256, b.data) "
            "FROM entries e JOIN blobs b ON b.sha256 = e.metadata_sha256"
        ).fetchall()
        times = connection.execute(
            "SELECT id, updated_at, created_at, last_user_at FROM sessions"
        ).fetchall()
        events = connection.execute(
            "SELECT session_id, seq, time FROM events"
        ).fetchall()
        connection.executemany(
            "UPDATE entries SET metadata = ?, metadata_sha256 = NULL WHERE session_id = ? AND seq = ?",
            ((raw.decode(), owner, seq) for owner, seq, raw in metadata),
        )
        connection.execute("DELETE FROM blob_refs")
        connection.execute("DELETE FROM blob_codecs")
        connection.execute("DELETE FROM blob_compaction")
        connection.executemany(
            "UPDATE blobs SET data = ? WHERE sha256 = ?",
            ((raw, sha) for sha, raw in blobs),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = '2000-01-01', created_at = '2000-01-01', last_user_at = '2000-01-01'"
        )
        connection.execute("UPDATE events SET time = '2000-01-01'")
    compact_database(database)
    with database.write() as connection:
        connection.executemany(
            "UPDATE sessions SET updated_at = ?, created_at = ?, last_user_at = ? WHERE id = ?",
            (
                (updated, created, user, owner)
                for owner, updated, created, user in times
            ),
        )
        connection.executemany(
            "UPDATE events SET time = ? WHERE session_id = ? AND seq = ?",
            ((time, owner, seq) for owner, seq, time in events),
        )
