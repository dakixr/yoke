"""Durable session index ownership and maintenance."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock, RLock

from yoke.session.repository.index import (
    repair_index_from_session_files,
    session_file_ids,
    session_index_entry,
)
from yoke.session.repository.index_cache import SessionIndexCache
from yoke.session.repository.load import (
    load_existing_record,
    reconcile_index_owned_metadata,
    scan_canonical_session_summary,
)
from yoke.session.repository.maintenance import prune_index_and_sessions
from yoke.session.repository.models import (
    SessionIndex,
    SessionIndexEntry,
    SessionRecord,
)
from yoke.session.repository.paths import (
    CURRENT_SESSION_SCHEMA_VERSION,
    SESSION_FILE_SUFFIX,
    SESSION_ID_PATTERN,
    SESSION_INDEX_NAME,
    SESSION_RETENTION_DAYS,
    session_path,
)

logger = logging.getLogger(__name__)


class SessionIndexService:
    """Own the index cache, locking, repair, and retention lifecycle."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cache = SessionIndexCache(directory / SESSION_INDEX_NAME)
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
                session_file_suffix=SESSION_FILE_SUFFIX,
                session_id_pattern=SESSION_ID_PATTERN,
                load_summary=self._load_summary,
            )
        )

    def prune(self) -> bool:
        return self._mutate(lambda index: self._prune_with_scan(index))

    def _prune_with_scan(self, index: SessionIndex) -> bool:
        existing_ids = session_file_ids(
            self.directory,
            session_file_suffix=SESSION_FILE_SUFFIX,
            session_id_pattern=SESSION_ID_PATTERN,
        )
        if existing_ids is None:
            return False
        return prune_index_and_sessions(
            directory=self.directory,
            session_path=lambda session_id: session_path(self.directory, session_id),
            index=index,
            retention_days=SESSION_RETENTION_DAYS,
            existing_session_ids=existing_ids,
        )

    def _replace(
        self, index: SessionIndex, record: SessionRecord, *, entry_count: int | None
    ) -> bool:
        index.sessions[record.id] = session_index_entry(
            record,
            path=session_path(self.directory, record.id),
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

    def _load_summary(self, session_id: str) -> tuple[SessionRecord, int]:
        path = session_path(self.directory, session_id)
        scanned = scan_canonical_session_summary(path, session_id)
        if scanned is not None:
            record, entry_count = scanned
            return (
                reconcile_index_owned_metadata(record, self.entry(session_id)),
                entry_count,
            )
        record = load_existing_record(
            self,
            session_id,
            path,
            current_schema_version=CURRENT_SESSION_SCHEMA_VERSION,
            update_index=False,
        )
        if record.version != CURRENT_SESSION_SCHEMA_VERSION:
            raise ValueError(f"Unsupported session schema version: {record.version}.")
        return record, len(record.conversation_entries)
