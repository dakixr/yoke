"""Small utilities for CLI session persistence."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


DATABASE_FILE_NAME = "yoke.db"


def default_session_directory() -> Path:
    """Return the directory for session file state that is not in the database.

    Workspace leases and uploads live here. ``YOKE_SESSION_DIR`` relocates it
    together with the database, which keeps tests and tools fully isolated.
    """
    override = os.getenv("YOKE_SESSION_DIR")
    if override:
        return Path(override)
    return Path.home() / ".yoke" / "sessions"


def default_database_path() -> Path:
    """Return the global database: YOKE_DB_PATH, YOKE_SESSION_DIR, or ~/.yoke."""
    override = os.getenv("YOKE_DB_PATH")
    if override:
        return Path(override).expanduser()
    session_override = os.getenv("YOKE_SESSION_DIR")
    if session_override:
        return Path(session_override) / DATABASE_FILE_NAME
    return Path.home() / ".yoke" / DATABASE_FILE_NAME


def database_path_for(directory: Path | None) -> Path:
    """Return the database that holds sessions for a session directory.

    The default session directory maps to the global database. Any other
    directory (tests, embedded SDK stores) keeps its own ``yoke.db`` inside it.
    """
    if directory is None:
        return default_database_path()
    resolved = directory.expanduser().resolve()
    default = default_session_directory().expanduser().resolve()
    if resolved == default:
        return default_database_path()
    return resolved / DATABASE_FILE_NAME


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
