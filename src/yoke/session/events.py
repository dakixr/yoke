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
        self._history_offsets: dict[str, dict[int, int]] = {}

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
                with path.open("rb") as handle:
                    target = after + 1
                    start_seq, start_offset = self._history_start_locked(
                        session_id,
                        target,
                    )
                    handle.seek(start_offset)
                    while True:
                        offset = handle.tell()
                        line = handle.readline()
                        if not line:
                            break
                        if not line.strip():
                            continue
                        try:
                            event = SessionEvent.model_validate_json(line)
                        except ValueError:
                            continue
                        if event.seq < start_seq:
                            continue
                        self._remember_history_offset_locked(
                            session_id,
                            event.seq,
                            offset,
                        )
                        if event.seq <= after:
                            continue
                        selected.append(event)
                        if len(selected) > limit:
                            return selected[:limit], True
            except OSError:
                return [], False
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
        last = self._tail_sequence(path)
        self._last_seq[session_id] = last
        return last

    def _history_start_locked(self, session_id: str, target: int) -> tuple[int, int]:
        offsets = self._history_offsets.get(session_id)
        if not offsets:
            return 1, 0
        candidates = [seq for seq in offsets if seq <= target]
        if not candidates:
            return 1, 0
        seq = max(candidates)
        return seq, offsets[seq]

    def _remember_history_offset_locked(
        self,
        session_id: str,
        seq: int,
        offset: int,
    ) -> None:
        offsets = self._history_offsets.setdefault(session_id, {})
        # Sparse checkpoints bound memory for huge journals. The first event
        # after a page cursor is also retained, which makes normal sequential
        # browser catch-up seek directly to its next page.
        if seq == 1 or seq % 128 == 1 or seq not in offsets and len(offsets) < 2:
            offsets.setdefault(seq, offset)
        if len(offsets) > 65_536:
            sparse = {
                checkpoint: value
                for checkpoint, value in offsets.items()
                if checkpoint == 1 or checkpoint % 1024 == 1
            }
            self._history_offsets[session_id] = sparse

    @staticmethod
    def _tail_sequence(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                window = min(size, 64 * 1024)
                while window:
                    handle.seek(size - window)
                    chunk = handle.read(window)
                    for line in reversed(chunk.splitlines()):
                        if not line.strip():
                            continue
                        try:
                            return SessionEvent.model_validate_json(line).seq
                        except ValueError:
                            continue
                    if window == size:
                        break
                    window = min(size, window * 2)
        except OSError:
            return 0
        return 0

    def _path(self, session_id: str) -> Path:
        return self.session_directory / "events" / f"{session_id}.jsonl"

    def _lock_for(self, session_id: str) -> Lock:
        with self._locks_lock:
            return self._locks.setdefault(session_id, Lock())
