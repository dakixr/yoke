"""Thread-safe cached reads and atomic writes for the session index."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from yoke._file_io import exclusive_file_lock
from yoke.cli.session.models import SessionIndex


INDEX_WRITE_ATTEMPTS = 6
INDEX_WRITE_RETRY_SECONDS = 0.02
type FileSignature = tuple[int, int, int, int, int]


class SessionIndexCache:
    """Cache parsed index snapshots while preserving safe replacement writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._signature: FileSignature | None = None
        self._snapshot: SessionIndex | None = None
        self._dirty = False

    def read(self) -> SessionIndex:
        """Return the cached immutable-by-convention index snapshot."""
        with self._lock:
            signature = self._file_signature()
            if self._snapshot is not None and signature == self._signature:
                return self._snapshot
            if signature is None:
                snapshot = SessionIndex()
            else:
                try:
                    snapshot = SessionIndex.model_validate_json(
                        self.path.read_text(encoding="utf-8")
                    )
                except (OSError, ValidationError):
                    return self._snapshot or SessionIndex()
            self._snapshot = snapshot
            self._signature = signature
            self._dirty = False
            return snapshot

    def write(self, index: SessionIndex) -> None:
        """Atomically replace the index and publish the new cached snapshot."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_file_lock(self._lock_path()):
                try:
                    self._replace(index)
                except OSError:
                    self._publish(index, dirty=True)
                    raise
                self._publish(index)

    def update(self, mutator: Callable[[SessionIndex], bool]) -> SessionIndex:
        """Apply one read-modify-write update under the shared index lock."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_file_lock(self._lock_path()):
                signature = self._file_signature()
                reuse_snapshot = (
                    self._snapshot is not None and signature == self._signature
                )
                retry_dirty_snapshot = self._dirty and reuse_snapshot
                current = (
                    self._snapshot
                    if reuse_snapshot and self._snapshot is not None
                    else self._read_disk()
                )
                self._publish(current, dirty=retry_dirty_snapshot)
                updated = current.model_copy(
                    update={"sessions": dict(current.sessions)}
                )
                if not mutator(updated) and not retry_dirty_snapshot:
                    return current
                try:
                    self._replace(updated)
                except OSError:
                    # The JSONL session record is authoritative. Keep the
                    # process-local projection usable and let maintenance retry
                    # the disposable disk index later.
                    self._publish(updated, dirty=True)
                    raise
                self._publish(updated)
                return updated

    def _read_disk(self) -> SessionIndex:
        signature = self._file_signature()
        if signature is None:
            return SessionIndex()
        try:
            return SessionIndex.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            return self._snapshot or SessionIndex()

    def _replace(self, index: SessionIndex) -> None:
        payload = index.model_dump_json(indent=2)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(payload)
            self._replace_with_retry(temporary)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _replace_with_retry(self, temporary: Path) -> None:
        last_error: PermissionError | None = None
        for attempt in range(INDEX_WRITE_ATTEMPTS):
            try:
                self._replace_once(temporary)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt + 1 < INDEX_WRITE_ATTEMPTS:
                    time.sleep(INDEX_WRITE_RETRY_SECONDS * (2**attempt))
        assert last_error is not None
        raise last_error

    def _replace_once(self, temporary: Path) -> None:
        temporary.replace(self.path)

    def _publish(self, index: SessionIndex, *, dirty: bool = False) -> None:
        self._snapshot = index
        self._signature = self._file_signature()
        self._dirty = dirty

    def _lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    def invalidate(self) -> None:
        """Force the next read to inspect the index file again."""
        with self._lock:
            self._signature = None
            self._snapshot = None
            self._dirty = False

    def _file_signature(self) -> FileSignature | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_ctime_ns,
            stat.st_mtime_ns,
            stat.st_size,
        )
