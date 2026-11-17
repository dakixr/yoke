"""Rows of the ``inputs`` table shared by admissions and the prompt queue.

The admission's typed phase owns the lifecycle. Its pending payload and queue
position project queued or paused inputs. The other columns index that phase
and its legacy admission fields. Legacy queue-only writers keep their pending
projection until schema migration gives the input an admission identity.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from yoke.session.repository.utils import database_path_for
from yoke.storage import Database, open_database


def session_database(session_directory: Path) -> Database:
    """Return the database that holds sessions for a session directory."""
    return open_database(database_path_for(session_directory))


def sync_input_row(
    connection: sqlite3.Connection, session_id: str, input_id: str
) -> None:
    """Recompute derived columns, deleting a row left with no payload."""
    row = connection.execute(
        "SELECT admission, pending FROM inputs WHERE session_id = ? AND id = ?",
        (session_id, input_id),
    ).fetchone()
    if row is None:
        return
    admission = json.loads(row[0]) if row[0] is not None else None
    pending = json.loads(row[1]) if row[1] is not None else None
    if admission is None and pending is None:
        connection.execute(
            "DELETE FROM inputs WHERE session_id = ? AND id = ?",
            (session_id, input_id),
        )
        return
    if admission is not None:
        delivery = admission.get("delivery")
    else:
        assert pending is not None
        delivery = "steer" if pending.get("kind") == "steering" else "queue"
    source = pending if pending is not None else admission
    assert source is not None
    connection.execute(
        "UPDATE inputs SET state = ?, delivery = ?, paused = ?, prompt = ?, "
        "admitted_seq = ?, promoted_seq = ?, phase = ? WHERE session_id = ? AND id = ?",
        (
            admission.get("state") if admission is not None else None,
            delivery,
            int(bool(pending.get("paused"))) if pending is not None else 0,
            str(source.get("prompt", "")),
            admission.get("admitted_seq") if admission is not None else None,
            admission.get("promoted_seq") if admission is not None else None,
            admission["phase"]["kind"]
            if admission and "phase" in admission
            else "paused"
            if pending and pending.get("paused")
            else "queued",
            session_id,
            input_id,
        ),
    )


def ensure_input_row(
    connection: sqlite3.Connection, session_id: str, input_id: str
) -> None:
    """Create an empty input row to receive a payload."""
    connection.execute(
        "INSERT INTO inputs (session_id, id, prompt) VALUES (?, ?, '') "
        "ON CONFLICT (session_id, id) DO NOTHING",
        (session_id, input_id),
    )
