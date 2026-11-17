"""Temporary directories short enough for Unix socket paths on every host.

macOS caps ``sockaddr_un.sun_path`` at 104 bytes including the terminator and
keeps ``TMPDIR`` under ``/var/folders/<long-id>/T``. A pytest ``tmp_path`` also
includes the test name, so daemon sockets there exceed the limit even though the
product is fine. Put socket directories under ``/tmp`` instead.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def short_temp_parent() -> Path | None:
    """Prefer /tmp on POSIX; None lets the OS pick where no short path exists."""
    if os.name == "posix" and Path("/tmp").is_dir():
        return Path("/tmp")
    return None


@contextmanager
def short_temp_dir(prefix: str = "yk-") -> Iterator[Path]:
    """Yield a temporary directory whose descendants fit ``sun_path``."""
    with tempfile.TemporaryDirectory(
        prefix=prefix, dir=short_temp_parent()
    ) as directory:
        yield Path(directory)
