"""Retention operations for the session repository."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from yoke.session.repository.models import SessionIndex
from yoke.session.repository.utils import parse_timestamp


def prune_index_and_sessions(
    *,
    directory: Path,
    session_path: Callable[[str], Path],
    index: SessionIndex,
    retention_days: int,
    existing_session_ids: set[str],
) -> bool:
    """Remove expired sessions and stale index entries."""
    del directory
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    changed = False
    for session_id, entry in list(index.sessions.items()):
        if session_id not in existing_session_ids:
            index.sessions.pop(session_id, None)
            changed = True
            continue
        last_activity = parse_timestamp(entry.updated_at) or parse_timestamp(
            entry.created_at
        )
        if entry.pinned or last_activity is None or last_activity >= cutoff:
            continue
        try:
            session_path(session_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        index.sessions.pop(session_id, None)
        changed = True
    return changed
