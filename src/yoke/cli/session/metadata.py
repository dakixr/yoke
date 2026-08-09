"""Constant-work session metadata persistence."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from yoke.cli.session.models import SessionRecord
from yoke.cli.session.utils import normalize_root
from yoke.cli.session.utils import normalize_title
from yoke.cli.session.utils import timestamp
from yoke.cli.session.writer import append_session_metadata

if TYPE_CHECKING:
    from yoke.cli.session.store import SessionStore


def update_loaded_provider_state(
    store: SessionStore,
    record: SessionRecord,
    *,
    root: Path | str | None,
    title: str | None,
    provider_name: str | None,
    model_id: str | None,
    reasoning_effort: str | None,
    context_window_tokens: int | None,
) -> SessionRecord:
    """Append provider metadata and return an updated loaded record."""
    changes: dict[str, object] = {
        "root": normalize_root(root) or record.root,
        "title": normalize_title(title) or record.title,
        "provider_name": provider_name,
        "model_id": model_id,
        "reasoning_effort": reasoning_effort,
        "context_window_tokens": context_window_tokens,
    }
    changes = {
        key: value for key, value in changes.items() if getattr(record, key) != value
    }
    if not changes and record.created_at is not None:
        return record
    now = timestamp()
    changes["updated_at"] = now
    if record.created_at is None:
        changes["created_at"] = now
    updated = record.model_copy(update=changes)
    path = store._session_path(record.id)
    store.directory.mkdir(parents=True, exist_ok=True)
    if path.exists():
        append_session_metadata(path, changes)
    else:
        store._write_session_record(updated)
    for key, value in changes.items():
        setattr(record, key, value)
    store._update_index(updated)
    return record
