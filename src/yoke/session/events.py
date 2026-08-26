"""Append-only public durable event journal for saved sessions."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
import secrets
from threading import Lock

from pydantic import BaseModel
from pydantic import Field


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
    """Process-local serialized writer over per-session JSONL journals."""

    def __init__(self, session_directory: Path) -> None:
        self.session_directory = session_directory.resolve()
        self._locks_lock = Lock()
        self._locks: dict[str, Lock] = {}
        self._last_seq: dict[str, int] = {}

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
        lock = self._lock_for(session_id)
        with lock:
            seq = self._last_sequence_locked(session_id) + 1
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
            path = self._path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json())
                handle.write("\n")
                handle.flush()
            self._last_seq[session_id] = seq
            return event

    def history(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> tuple[list[SessionEvent], bool]:
        """Read a finite public event page after an exclusive sequence cursor."""
        path = self._path(session_id)
        if not path.exists():
            return [], False
        selected: list[SessionEvent] = []
        with self._lock_for(session_id):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return [], False
        for line in lines:
            if not line.strip():
                continue
            try:
                event = SessionEvent.model_validate_json(line)
            except ValueError:
                continue
            if event.seq <= after:
                continue
            selected.append(event)
            if len(selected) > limit:
                return selected[:limit], True
        return selected, False

    def latest_sequence(self, session_id: str) -> int:
        """Return the latest durable sequence currently present for a session."""
        with self._lock_for(session_id):
            return self._last_sequence_locked(session_id)

    def _last_sequence_locked(self, session_id: str) -> int:
        cached = self._last_seq.get(session_id)
        if cached is not None:
            return cached
        path = self._path(session_id)
        last = 0
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        event = SessionEvent.model_validate_json(line)
                    except ValueError:
                        continue
                    last = max(last, event.seq)
            except OSError:
                pass
        self._last_seq[session_id] = last
        return last

    def _path(self, session_id: str) -> Path:
        return self.session_directory / "events" / f"{session_id}.jsonl"

    def _lock_for(self, session_id: str) -> Lock:
        with self._locks_lock:
            return self._locks.setdefault(session_id, Lock())

