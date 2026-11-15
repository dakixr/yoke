"""Append-only public durable event journal for saved sessions."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_core import from_json

from yoke.session.inputs import session_database
from yoke.session.repository.rows import encode_json


class SessionEvent(BaseModel):
    """One public durable event with a session-local monotonic sequence."""

    id: str
    type: str
    time: str
    session_id: str
    seq: int
    version: int = 1
    data: dict[str, object] = Field(default_factory=dict)
    location: str | None = None


class SessionEventJournal:
    """Durable per-session event sequences in the shared database.

    An append inside an open write transaction (for example a prompt
    admission) commits together with it. ``after_commit`` defers live
    publication until the event is durable.
    """

    def __init__(self, session_directory: Path) -> None:
        self.session_directory = session_directory.resolve()
        self.database = session_database(self.session_directory)

    def append(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, object] | None = None,
        *,
        location: str | None = None,
        version: int = 1,
    ) -> SessionEvent:
        """Append and return one durable public event."""
        with self.database.write() as connection:
            seq = (
                connection.execute(
                    "SELECT coalesce(max(seq), 0) FROM events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                + 1
            )
            event = SessionEvent(
                id=f"evt_{secrets.token_hex(12)}",
                type=event_type,
                time=datetime.now(UTC).isoformat(),
                session_id=session_id,
                seq=seq,
                version=version,
                data=data or {},
                location=location,
            )
            connection.execute(
                "INSERT INTO events (session_id, seq, id, type, time, version, "
                "location, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    event.id,
                    event.type,
                    event.time,
                    event.version,
                    event.location,
                    encode_json(
                        event.model_dump(mode="json", include={"data"})["data"]
                    ),
                ),
            )
            return event

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` once the current write commits (now outside one)."""
        self.database.after_commit(callback)

    def history(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> tuple[list[SessionEvent], bool]:
        """Read a finite public event page after an exclusive sequence cursor."""
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT id, type, time, seq, version, location, data FROM events "
                "WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (session_id, after, limit + 1),
            ).fetchall()
        events = [
            SessionEvent(
                id=row[0],
                type=row[1],
                time=row[2],
                session_id=session_id,
                seq=row[3],
                version=row[4],
                location=row[5],
                data=from_json(row[6]),
            )
            for row in rows
        ]
        return events[:limit], len(events) > limit

    def latest_sequence(self, session_id: str) -> int:
        """Return the latest durable sequence currently present for a session."""
        with self.database.read() as connection:
            return int(
                connection.execute(
                    "SELECT coalesce(max(seq), 0) FROM events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
