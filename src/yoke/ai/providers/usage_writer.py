"""Durable JSONL writes for provider usage records."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from yoke._file_io import exclusive_file_lock

WRITE_ATTEMPTS = 3
WRITE_RETRY_SECONDS = 0.02


class UsageLogWriteError(OSError):
    """Raised when a provider usage record cannot be persisted durably."""


def append_json_line(path: Path, record: dict[str, object]) -> None:
    """Append one JSON record with locking, retries, and durable flushing."""
    encoded = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    last_error: OSError | None = None
    for attempt in range(WRITE_ATTEMPTS):
        try:
            _append_once(path, encoded)
            return
        except UsageLogWriteError:
            raise
        except OSError as exc:
            last_error = exc
            if attempt + 1 < WRITE_ATTEMPTS:
                time.sleep(WRITE_RETRY_SECONDS * (2**attempt))
    raise UsageLogWriteError(
        f"Could not persist provider usage metric to {path} after "
        f"{WRITE_ATTEMPTS} attempts."
    ) from last_error


def _append_once(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with exclusive_file_lock(lock_path):
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            original_size = os.fstat(descriptor).st_size
            try:
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
                _fsync_directory(path.parent)
            except OSError:
                _rollback_write(descriptor, original_size)
                raise
        finally:
            os.close(descriptor)


def _rollback_write(descriptor: int, original_size: int) -> None:
    """Restore the pre-attempt EOF before a failed append is retried."""
    try:
        os.ftruncate(descriptor, original_size)
        os.fsync(descriptor)
    except OSError as exc:
        raise UsageLogWriteError(
            "Could not roll back a failed provider usage metric append."
        ) from exc


def _fsync_directory(path: Path) -> None:
    """Persist the daily file's directory entry on POSIX systems."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Usage metric write made no forward progress.")
        remaining = remaining[written:]
