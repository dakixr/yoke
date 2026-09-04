"""Retention operations for the CLI session store."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
import os
from typing import TYPE_CHECKING

from yoke.cli.session.utils import parse_timestamp

if TYPE_CHECKING:
    from yoke.cli.session.models import SessionIndex
    from yoke.cli.session.store import SessionStore


def prune_index_and_sessions(
    store: SessionStore,
    *,
    index: SessionIndex,
    retention_days: int,
    exclude_session_id: str | None,
    existing_session_ids: set[str] | None = None,
) -> bool:
    """Remove expired sessions and stale index entries."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    changed = False
    for session_id, entry in list(index.sessions.items()):
        if session_id == exclude_session_id:
            continue
        session_path = None
        if existing_session_ids is None:
            session_path = store._existing_session_path(session_id)
            missing = session_path is None
        else:
            missing = os.path.normcase(session_id) not in existing_session_ids
        if missing:
            index.sessions.pop(session_id, None)
            changed = True
            continue
        last_activity = parse_timestamp(entry.updated_at) or parse_timestamp(
            entry.created_at
        )
        if entry.pinned or last_activity is None or last_activity >= cutoff:
            continue
        session_path = session_path or store._session_path(session_id)
        try:
            session_path.unlink()
        except FileNotFoundError:
            index.sessions.pop(session_id, None)
            changed = True
            continue
        except OSError:
            continue
        index.sessions.pop(session_id, None)
        changed = True
    return changed
