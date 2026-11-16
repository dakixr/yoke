"""Resumable storage compaction with image work outside short transactions."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from yoke.storage.blob_codec import collect_blobs, prepare_blob
from yoke.storage.database import Database
from yoke.storage.entry_transform import transform_entry

# Shared entry hashes are only rewritten when *all* owners are quiet. Admission
# settlement is authoritative even for turns older than the activity window.
_BUSY = """
   julianday(coalesce(s.updated_at, s.created_at, 'now')) > julianday('now', '-10 minutes')
   OR julianday(s.last_user_at) > julianday('now', '-10 minutes')
   OR EXISTS (SELECT 1 FROM events ev WHERE ev.session_id = s.id
              AND julianday(ev.time) > julianday('now', '-10 minutes'))
   OR EXISTS (SELECT 1 FROM inputs i WHERE i.session_id = s.id
              AND i.admission IS NOT NULL
              AND json_extract(i.admission, '$.state') = 'promoted'
              AND coalesce(json_extract(i.admission, '$.settled'), 0) = 0)
   OR coalesce((SELECT max(seq) FROM events WHERE session_id = s.id
                AND type = 'session.compaction.started'), 0) >
      coalesce((SELECT max(seq) FROM events WHERE session_id = s.id
                AND type = 'session.compaction.ended'), 0)
"""
_QUIET = (
    """
NOT EXISTS (
 SELECT 1 FROM entries owner JOIN sessions s ON s.id = owner.session_id
 WHERE (owner.message_sha256 = b.sha256 OR owner.metadata_sha256 = b.sha256)
 AND ("""
    + _BUSY
    + "))"
)
_METADATA = (
    """
SELECT e.session_id, e.seq, e.metadata FROM entries e JOIN sessions s ON s.id = e.session_id
WHERE e.metadata IS NOT NULL AND NOT coalesce(("""
    + _BUSY
    + "), 0)"
)
_ELIGIBLE = (
    """
FROM blobs b
WHERE NOT EXISTS (SELECT 1 FROM blob_compaction c WHERE c.sha256 = b.sha256)
AND (EXISTS (SELECT 1 FROM entries WHERE message_sha256 = b.sha256)
     OR EXISTS (SELECT 1 FROM entries WHERE metadata_sha256 = b.sha256))
AND """
    + _QUIET
)
_CANDIDATES = "SELECT b.sha256, yoke_blob_data(b.sha256, b.data) " + _ELIGIBLE


@dataclass
class CompactionResult:
    """Counts from committed batches, safe to add across resumptions."""

    processed: int = 0
    entries_rewritten: int = 0
    images_externalized: int = 0
    images_reencoded: int = 0
    failures_inline: int = 0
    metadata_rewritten: int = 0

    def add(self, other: CompactionResult) -> None:
        """Accumulate a completed batch."""
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


def compact_batch(
    database: Database, *, limit: int = 2, nonblocking: bool = False
) -> CompactionResult:
    """Prepare bounded work, then recheck quietness in each write transaction.

    A competing writer wins over idle maintenance. Each entry's transform is
    committed atomically with references, session counters and cache revisions.
    """
    result = CompactionResult()
    started = time.monotonic()
    with database.read() as connection:
        candidates = connection.execute(_CANDIDATES + " LIMIT ?", (limit,)).fetchall()
    for old_sha, raw in candidates:
        if nonblocking and time.monotonic() - started > 0.2:
            break
        transformed, images = transform_entry(raw)
        prepared = prepare_blob(transformed, inline_urls=images.failed_urls)
        try:
            with (
                database.write_nowait()
                if nonblocking
                else database.write() as connection
            ):
                eligible = connection.execute(
                    "SELECT 1 " + _ELIGIBLE + " AND b.sha256 = ?", (old_sha,)
                ).fetchone()
                if eligible is None:
                    continue
                owners = connection.execute(
                    "SELECT session_id, count(*) FROM entries WHERE message_sha256 = ? GROUP BY session_id",
                    (old_sha,),
                ).fetchall()
                metadata_owners = connection.execute(
                    "SELECT DISTINCT session_id FROM entries WHERE metadata_sha256 = ?",
                    (old_sha,),
                ).fetchall()
                new_sha = prepared.insert(connection, replace_inline=True)
                connection.execute(
                    "UPDATE entries SET message_sha256 = ? WHERE message_sha256 = ?",
                    (new_sha, old_sha),
                )
                connection.execute(
                    "UPDATE entries SET metadata_sha256 = ? WHERE metadata_sha256 = ?",
                    (new_sha, old_sha),
                )
                affected = {
                    owner: count * (len(transformed) - len(raw))
                    for owner, count in owners
                }
                affected.update(
                    {owner: affected.get(owner, 0) for (owner,) in metadata_owners}
                )
                for owner, delta in affected.items():
                    connection.execute(
                        "UPDATE sessions SET message_bytes = message_bytes + ?, rev = rev + 1, rewrite_rev = rev + 1 WHERE id = ?",
                        (delta, owner),
                    )
                    connection.execute(
                        "UPDATE entries SET rev = (SELECT rev FROM sessions WHERE id = ?) WHERE session_id = ? AND (message_sha256 = ? OR metadata_sha256 = ?)",
                        (owner, owner, new_sha, new_sha),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO blob_compaction VALUES (?)", (new_sha,)
                )
                result.processed += 1
                result.entries_rewritten += sum(count for _, count in owners)
                externalized = (
                    len(prepared.refs)
                    if connection.execute(
                        "SELECT 1 FROM blob_codecs WHERE sha256 = ?", (new_sha,)
                    ).fetchone()
                    else 0
                )
                result.images_externalized += externalized
                result.images_reencoded += images.reencoded
                result.failures_inline += (
                    images.failures
                    + prepared.failures
                    + len(prepared.refs)
                    - externalized
                )
        except sqlite3.OperationalError as exc:
            if not nonblocking or exc.sqlite_errorcode not in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise
            break
    # Legacy whole-metadata replacements are independent blobs, with their own
    # reachability roots. Do not merge them into shared entry blobs.
    if result.processed < limit:
        result.metadata_rewritten = _convert_metadata(
            database, limit - result.processed, nonblocking
        )
    return result


def _convert_metadata(database: Database, limit: int, nonblocking: bool) -> int:
    with database.read() as connection:
        rows = connection.execute(_METADATA + " LIMIT ?", (limit,)).fetchall()
    count = 0
    for owner, seq, metadata in rows:
        # Stage only. The next pass transforms the blob after the same quiet
        # checks as entry blobs, including recent durable events.
        prepared = prepare_blob(metadata.encode())
        try:
            with (
                database.write_nowait()
                if nonblocking
                else database.write() as connection
            ):
                current = connection.execute(
                    _METADATA + " AND e.session_id = ? AND e.seq = ?",
                    (owner, seq),
                ).fetchone()
                if current != (owner, seq, metadata):
                    continue
                digest = prepared.insert(connection)
                connection.execute(
                    "UPDATE entries SET metadata = NULL, metadata_sha256 = ? WHERE session_id = ? AND seq = ?",
                    (digest, owner, seq),
                )
                count += 1
        except sqlite3.OperationalError as exc:
            if not nonblocking or exc.sqlite_errorcode not in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise
            break
    return count


def compact_database(database: Database) -> CompactionResult:
    """Finish eligible work, collect orphan images, and reclaim physical space."""
    total = CompactionResult()
    while True:
        batch = compact_batch(database, limit=16)
        total.add(batch)
        if not batch.processed and not batch.metadata_rewritten:
            break
    with database.write() as connection:
        while collect_blobs(connection, limit=1024):
            pass
    # VACUUM owns a separate connection; no open transaction or read snapshot.
    connection = sqlite3.connect(database.path, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return total
