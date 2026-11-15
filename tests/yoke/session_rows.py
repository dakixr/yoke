"""Test helpers that observe stored session rows directly."""

from __future__ import annotations

import json
from typing import Any

from yoke.session import SessionStore


def stored_session_bytes(store: SessionStore, session_id: str) -> bytes:
    """Return every stored column of a session and its entries.

    The database equivalent of reading a JSONL session file's bytes: any write
    to the session, including a no-op metadata rewrite, changes ``rev``.
    """
    with store.database.read() as connection:
        connection.row_factory = None
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        entries = connection.execute(
            "SELECT seq, id, parent_id, kind, blobs.data, metadata, rev "
            "FROM entries JOIN blobs ON blobs.sha256 = entries.message_sha256 WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    if session is None:
        raise FileNotFoundError(session_id)
    payload = {
        "session": list(session),
        "entries": [
            [*row[:4], bytes(row[4]).decode("utf-8"), *row[5:]] for row in entries
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def stored_entry_rows(store: SessionStore, session_id: str) -> list[tuple[Any, ...]]:
    """Return the stored entry rows (history, without session metadata)."""
    with store.database.read() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT seq, id, parent_id, kind, blobs.data, metadata, rev "
                "FROM entries JOIN blobs ON blobs.sha256 = entries.message_sha256 WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
        ]


def stored_entry_bytes(store: SessionStore, session_id: str) -> bytes:
    """Return the stored entry rows as bytes for equality checks."""
    rows = stored_entry_rows(store, session_id)
    return json.dumps(
        [[*row[:4], bytes(row[4]).decode("utf-8"), *row[5:]] for row in rows],
        ensure_ascii=False,
    ).encode("utf-8")
