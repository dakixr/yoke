"""Build identity used to detect a client and daemon running different code."""

from __future__ import annotations

import os
import subprocess
from functools import cache
from pathlib import Path

from yoke._version import __version__

# Bump when a client of the previous protocol can no longer drive the daemon.
PROTOCOL_VERSION = "1"

# Test hooks: simulate two checkouts without building two trees.
BUILD_ID_ENV = "YOKE_DAEMON_BUILD_ID"
PROTOCOL_ENV = "YOKE_DAEMON_PROTOCOL"


def build_id() -> str:
    """Return ``version[+gHEAD[.dirty]]`` for the code this process imported."""
    return os.environ.get(BUILD_ID_ENV) or _source_build_id()


def protocol_version() -> str:
    return os.environ.get(PROTOCOL_ENV) or PROTOCOL_VERSION


@cache
def _source_build_id() -> str:
    source = Path(__file__).resolve().parent
    head = _git(source, "rev-parse", "HEAD")
    if not head:
        return __version__
    dirty = _git(source, "status", "--porcelain", "--untracked-files=normal")
    return f"{__version__}+g{head[:12]}{'.dirty' if dirty else ''}"


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
