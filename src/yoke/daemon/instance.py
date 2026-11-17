"""Single-instance lock and the ``daemon.json`` discovery record.

Liveness is decided by the lock, never by a pid: the kernel drops a file lock
when its holder dies, so a stale socket or record left by a killed daemon is
simply overwritten by the next daemon that wins the lock.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time
from dataclasses import asdict, dataclass

from yoke.daemon.paths import DaemonPaths


def _lock(fd: int, *, shared: bool = False) -> None:
    if sys.platform == "win32":
        import msvcrt

        # A byte beyond EOF can be locked. Never write into a held lock.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, mode | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _contended(exc: OSError) -> bool:
    if sys.platform == "win32":
        return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
    return isinstance(exc, BlockingIOError)


@dataclass(frozen=True, slots=True)
class DaemonInfo:
    """What a daemon publishes about itself in ``daemon.json``."""

    pid: int
    build_id: str
    protocol_version: str
    socket: str
    port: int | None
    token: str
    idle_exit: bool
    started_at: float


class InstanceLock:
    """An exclusive OS file lock on ``daemon.lock`` held for the daemon's lifetime."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    @classmethod
    def acquire(
        cls, paths: DaemonPaths, *, wait_seconds: float = 0.5
    ) -> InstanceLock | None:
        """Take the lock, or return ``None`` when another daemon holds it.

        A short retry absorbs momentary holders such as ``yoke daemon status``
        probing the lock; a live daemon holds it far longer than this.
        """
        paths.ensure()
        fd = os.open(
            paths.lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                _lock(fd)
            except OSError as exc:
                if not _contended(exc):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.02)
                continue
            return cls(fd)

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                _unlock(fd)
            finally:
                os.close(fd)


def daemon_alive(paths: DaemonPaths) -> bool:
    """Return whether some process holds the daemon lock right now."""
    try:
        fd = os.open(paths.lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    try:
        _lock(fd, shared=True)
        _unlock(fd)
    except OSError as exc:
        if not _contended(exc):
            raise
        return True
    finally:
        os.close(fd)
    return False


def write_info(paths: DaemonPaths, info: DaemonInfo) -> None:
    """Atomically replace ``daemon.json`` with a private (0600) record."""
    temporary = paths.info.with_name(f".{paths.info.name}.{os.getpid()}.tmp")
    fd = os.open(
        temporary,
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if sys.platform == "win32":
            from yoke.daemon.windows import protect_path

            protect_path(temporary)
        else:
            os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(asdict(info), indent=2).encode() + b"\n")
    finally:
        os.close(fd)
    os.replace(temporary, paths.info)


def read_info(paths: DaemonPaths) -> DaemonInfo | None:
    """Read ``daemon.json``; ``None`` when it is missing or unreadable."""
    try:
        raw = json.loads(paths.info.read_text())
        return DaemonInfo(**raw)
    except (OSError, ValueError, TypeError):
        return None


def remove_info(paths: DaemonPaths, *, pid: int) -> None:
    """Delete ``daemon.json`` if it still describes daemon *pid*."""
    current = read_info(paths)
    if current is not None and current.pid == pid:
        paths.info.unlink(missing_ok=True)
