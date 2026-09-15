"""Portable file locking and atomic replacement for local persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import suppress
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile
from threading import Lock
from weakref import WeakValueDictionary


@dataclass(slots=True, weakref_slot=True)
class _WindowsReaders:
    """One process-local reader group backed by one cross-process lock."""

    lock: Lock = field(default_factory=Lock)
    count: int = 0
    context: AbstractContextManager[None] | None = None


_windows_readers: WeakValueDictionary[str, _WindowsReaders] = WeakValueDictionary()
_windows_readers_lock = Lock()


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
    with file_lock(path):
        yield


@contextmanager
def file_lock(
    path: Path, *, shared: bool = False, blocking: bool = True
) -> Iterator[None]:
    """Hold a process-safe lease, optionally failing instead of waiting.

    Windows readers in one process share a reference-counted lease; different
    processes conservatively serialize readers. POSIX also shares across
    processes. Every platform excludes an exclusive writer.
    """
    if os.name == "nt" and shared:
        with _windows_shared_file_lock(path, blocking=blocking):
            yield
        return
    with _descriptor_file_lock(path, shared=shared, blocking=blocking):
        yield


@contextmanager
def _windows_shared_file_lock(path: Path, *, blocking: bool) -> Iterator[None]:
    """Retain the native lock through the last nested or cross-thread reader."""
    key = os.path.normcase(str(path.absolute()))
    with _windows_readers_lock:
        readers = _windows_readers.get(key)
        if readers is None:
            readers = _WindowsReaders()
            _windows_readers[key] = readers
    # Never hold the registry lock while waiting on a file lock. Independent
    # paths must be able to release their readers on another thread.
    with readers.lock:
        if readers.count == 0:
            context = _descriptor_file_lock(path, shared=False, blocking=blocking)
            context.__enter__()
            readers.context = context
        readers.count += 1
    try:
        yield
    finally:
        with readers.lock:
            readers.count -= 1
            if readers.count == 0:
                context = readers.context
                readers.context = None
                assert context is not None
                context.__exit__(None, None, None)


@contextmanager
def _descriptor_file_lock(
    path: Path, *, shared: bool, blocking: bool
) -> Iterator[None]:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        _lock_descriptor(descriptor, shared=shared, blocking=blocking)
        locked = True
        yield
    finally:
        try:
            if locked:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _lock_descriptor(
    descriptor: int, *, shared: bool = False, blocking: bool = True
) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(descriptor, mode if blocking else mode | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
