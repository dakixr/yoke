"""Local advisory exclusion for ACP peers sharing one native Yoke session."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from yoke.acp.native import fail


@contextmanager
def session_owner(origin: str, session_id: str) -> Iterator[None]:
    """Reject concurrent local ACP owners for one native session."""
    root = Path(tempfile.gettempdir()) / f"yoke-acp-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not root.is_dir()
        or root.is_symlink()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise fail("ACP ownership directory is unsafe")
    name = hashlib.sha256(f"{origin}\0{session_id}".encode()).hexdigest()
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise fail("Session already has another local ACP owner") from None
        yield
    finally:
        os.close(fd)
    # Do not unlink the lock file: another process may already hold its inode.
