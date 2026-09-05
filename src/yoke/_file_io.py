"""Portable file locking and atomic replacement for local persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import suppress
import os
from pathlib import Path
import tempfile


def atomic_write_text(path: Path, payload: str, *, fsync: bool = False) -> None:
    """Replace a text file, optionally syncing its data before publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        with handle:
            handle.write(payload)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a file lock across threads and processes, closing it on every exit."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        _lock_descriptor(descriptor)
        locked = True
        yield
    finally:
        try:
            if locked:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
