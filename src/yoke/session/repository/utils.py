"""Small utilities for CLI session persistence."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from pathlib import Path


def default_session_directory() -> Path:
    """Return the default directory used for CLI session records."""
    override = os.getenv("YOKE_SESSION_DIR")
    if override:
        return Path(override)
    return Path.home() / ".yoke" / "sessions"


def new_session_id() -> str:
    """Return a unique human-sortable session id."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def new_unique_session_id(exists: Callable[[str], bool]) -> str:
    """Return a new session id that is not present in a store."""
    while True:
        session_id = new_session_id()
        if not exists(session_id):
            return session_id


def fork_session_title(title: str | None) -> str | None:
    """Return the default title for a forked session."""
    normalized = normalize_title(title)
    if normalized is None:
        return None
    return fallback_session_title(f"{normalized} (fork)")


def fallback_session_title(prompt: str) -> str:
    """Build a compact fallback title from the user's prompt."""
    title = " ".join(prompt.split()).strip()
    if not title:
        return "Untitled session"
    return title if len(title) <= 80 else title[:77].rstrip() + "..."


def timestamp() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, returning None for empty/invalid values."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_root(root: Path | str | None) -> str | None:
    """Normalize a session root to an absolute string path."""
    if root is None:
        return None
    return str(Path(root).resolve())


def normalize_title(title: str | None) -> str | None:
    """Normalize a session title for storage."""
    if title is None:
        return None
    normalized = " ".join(title.split()).strip()
    return normalized or None
