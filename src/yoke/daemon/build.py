"""Build identity used to detect a client and daemon running different code."""

from __future__ import annotations

import hashlib
import os
import re
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
    """Return ``version[+gHEAD[.dirty.HASH]]`` for the code this process imported."""
    return os.environ.get(BUILD_ID_ENV) or _source_build_id()


def protocol_version() -> str:
    return os.environ.get(PROTOCOL_ENV) or PROTOCOL_VERSION


def fresh_build_id() -> str:
    """Return the identity a new process would load from disk, without caching."""
    return os.environ.get(BUILD_ID_ENV) or _disk_build_id()


@cache
def _source_build_id() -> str:
    return _disk_build_id()


def _disk_build_id() -> str:
    source = Path(__file__).resolve().parent
    version_file = source.parent / "_version.py"
    version = __version__
    try:
        match = re.search(r'__version__ = ["\']([^"\']+)', version_file.read_text())
        if match:
            version = match[1]
    except OSError:
        pass
    head = _git(source, "rev-parse", "HEAD")
    if not head:
        return version
    # Hash actual tracked changes and untracked contents. A boolean dirty flag
    # conflates different edits and can leave a daemon running obsolete code.
    changes = _git(source, "diff", "HEAD", "--binary", "--no-ext-diff")
    root = Path(_git(source, "rev-parse", "--show-toplevel"))
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    digest = hashlib.sha256(changes.encode())
    for name in untracked.split("\0"):
        if not name:
            continue
        digest.update(name.encode() + b"\0")
        try:
            digest.update(hashlib.sha256((root / name).read_bytes()).digest())
        except OSError:
            digest.update(b"<unreadable>")
    suffix = f".dirty.{digest.hexdigest()[:16]}" if changes or untracked else ""
    return f"{version}+g{head[:12]}{suffix}"


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            cwd=cwd.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
