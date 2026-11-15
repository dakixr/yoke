"""State owner for HTTP session topology snapshots."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable

from yoke.http.reads.message_index.models import MessageIndexSnapshot


class SnapshotCache:
    """Bounded snapshot cache with per-session and warming coordination."""

    def __init__(self, max_sessions: int) -> None:
        self._max_sessions = max_sessions
        self._lock = Lock()
        self._snapshots: OrderedDict[str, MessageIndexSnapshot] = OrderedDict()
        self._session_locks: dict[str, Lock] = {}
        self._warming: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="yoke-http-read-index"
        )

    def get(self, session_id: str) -> MessageIndexSnapshot | None:
        with self._lock:
            snapshot = self._snapshots.get(session_id)
            if snapshot is not None:
                self._snapshots.move_to_end(session_id)
            return snapshot

    def put(self, session_id: str, snapshot: MessageIndexSnapshot) -> None:
        with self._lock:
            self._snapshots[session_id] = snapshot
            self._snapshots.move_to_end(session_id)
            while len(self._snapshots) > self._max_sessions:
                self._snapshots.popitem(last=False)

    def session_lock(self, session_id: str) -> Lock:
        with self._lock:
            return self._session_locks.setdefault(session_id, Lock())

    def start_warming(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._warming:
                return False
            self._warming.add(session_id)
            return True

    def finish_warming(self, session_id: str) -> None:
        with self._lock:
            self._warming.discard(session_id)

    def submit(self, callback: Callable[[], object]) -> Future[object]:
        return self._executor.submit(callback)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
