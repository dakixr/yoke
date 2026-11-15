"""Durable session index ownership and maintenance."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
import re
from threading import Lock
from threading import RLock
import time

from yoke.session.repository.index import repair_index_from_session_files
from yoke.session.repository.index import session_file_ids
from yoke.session.repository.index import session_index_entry
from yoke.session.repository.index_cache import SessionIndexCache
from yoke.session.repository.models import SessionIndex
from yoke.session.repository.models import SessionIndexEntry
from yoke.session.repository.models import SessionRecord
from yoke.session.repository.maintenance import prune_index_and_sessions

logger = logging.getLogger(__name__)


class SessionIndexService:
    """Own the index cache, locking, repair, and retention lifecycle."""

    def __init__(
        self,
        directory: Path,
        *,
        index_path: Callable[[], Path],
        session_path: Callable[[str], Path],
        load_summary: Callable[[str], tuple[SessionRecord, int]],
        retention_days: int,
        session_file_suffix: str,
        session_id_pattern: re.Pattern[str],
        list_session_ids: Callable[[Path], set[str] | None] | None = None,
    ) -> None:
        self.directory = directory
        self._session_path = session_path
        self._load_summary = load_summary
        self._retention_days = retention_days
        self._session_file_suffix = session_file_suffix
        self._session_id_pattern = session_id_pattern
        self._list_session_ids = list_session_ids
        self._cache = SessionIndexCache(index_path())
        self._lock = RLock()
        self._maintenance_lock = Lock()
        self._last_maintenance_at = float("-inf")

    def read(self) -> SessionIndex:
        return self._cache.read()

    def entry(self, session_id: str) -> SessionIndexEntry | None:
        return self.read().sessions.get(session_id)

    def update(self, record: SessionRecord, *, entry_count: int | None = None) -> bool:
        return self._mutate(
            lambda index: self._replace(index, record, entry_count=entry_count)
        )

    def update_metadata(
        self, record: SessionRecord, *, entry_count: int | None = None
    ) -> bool:
        def mutate(index: SessionIndex) -> bool:
            existing = index.sessions.get(record.id)
            count = (
                entry_count
                if entry_count is not None
                else existing.entry_count
                if existing is not None and existing.entry_count is not None
                else len(record.conversation_entries)
            )
            return self._replace(index, record, entry_count=count)

        return self._mutate(mutate)

    def maintain(self, *, force: bool = False) -> None:
        if not force:
            self.maintain_if_due()
            return
        with self._maintenance_lock:
            self._maintain_once()

    def maintain_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance_at < 5.0:
            return
        with self._maintenance_lock:
            if time.monotonic() - self._last_maintenance_at < 5.0:
                return
            self._maintain_once()

    def _maintain_once(self) -> None:
        repaired = self.repair()
        pruned = self.prune()
        if repaired and pruned:
            self._last_maintenance_at = time.monotonic()

    def repair(self) -> bool:
        return self._mutate(
            lambda index: repair_index_from_session_files(
                directory=self.directory,
                index=index,
                session_file_suffix=self._session_file_suffix,
                session_id_pattern=self._session_id_pattern,
                load_summary=self._load_summary,
            )
        )

    def prune(self) -> bool:
        return self._mutate(lambda index: self._prune_with_scan(index))

    def _prune_with_scan(self, index: SessionIndex) -> bool:
        existing_ids = (
            self._list_session_ids(self.directory)
            if self._list_session_ids is not None
            else session_file_ids(
                self.directory,
                session_file_suffix=self._session_file_suffix,
                session_id_pattern=self._session_id_pattern,
            )
        )
        if existing_ids is None:
            return False
        return prune_index_and_sessions(
            directory=self.directory,
            session_path=self._session_path,
            index=index,
            retention_days=self._retention_days,
            existing_session_ids=existing_ids,
        )

    def _replace(
        self, index: SessionIndex, record: SessionRecord, *, entry_count: int | None
    ) -> bool:
        index.sessions[record.id] = session_index_entry(
            record,
            path=self._session_path(record.id),
            entry_count=entry_count,
        )
        return True

    def _mutate(self, mutator: Callable[[SessionIndex], bool]) -> bool:
        with self._lock:
            try:
                self._cache.update(mutator)
                return True
            except OSError:
                self._last_maintenance_at = float("-inf")
                logger.warning(
                    "Session index update failed; durable session files will repair it.",
                    exc_info=True,
                )
                return False
