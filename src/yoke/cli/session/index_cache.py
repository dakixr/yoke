"""Thread-safe cached reads and atomic writes for the session index."""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from yoke.cli.session.models import SessionIndex


class SessionIndexCache:
    """Cache parsed index snapshots while preserving safe replacement writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._signature: tuple[int, int] | None = None
        self._snapshot: SessionIndex | None = None

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
            return snapshot

    def write(self, index: SessionIndex) -> None:
        """Atomically replace the index and publish the new cached snapshot."""
        payload = index.model_dump_json(indent=2)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(self.path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            self._snapshot = index
            self._signature = self._file_signature()

    def invalidate(self) -> None:
        """Force the next read to inspect the index file again."""
        with self._lock:
            self._signature = None
            self._snapshot = None

    def _file_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size
