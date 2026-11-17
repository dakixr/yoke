"""Publish committed events in journal order across concurrent storage workers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from yoke.session.events import SessionEvent, SessionEventJournal


class DurablePublication:
    """Own publication cursors independently of transaction callback scheduling."""

    def __init__(
        self, journal: SessionEventJournal, publish: Callable[[SessionEvent], None]
    ) -> None:
        self.journal = journal
        self.publish = publish
        self._lock = Lock()
        self._cursors: dict[str, int] = {}

    def prepare(self, session_id: str) -> None:
        """Start at the existing journal tail before the first local append."""
        with self._lock:
            # Reading uses the caller's transaction or a WAL snapshot, never
            # acquires the database writer lock. A deleted/recreated session
            # starts its journal again at zero.
            sequence = self.journal.latest_sequence(session_id)
            if session_id not in self._cursors or sequence < self._cursors[session_id]:
                self._cursors[session_id] = sequence

    def committed(self, event: SessionEvent) -> None:
        """A later callback also flushes any earlier committed events it overtook."""
        session_id, through = event.session_id, event.seq
        with self._lock:
            cursor = self._cursors[session_id]
            if through == cursor + 1:
                self.publish(event)
                self._cursors[session_id] = through
                return
            while cursor < through:
                events, _ = self.journal.history(
                    session_id, after=cursor, limit=min(200, through - cursor)
                )
                if not events:
                    return
                for event in events:
                    self.publish(event)
                    cursor = event.seq
                    self._cursors[session_id] = cursor
