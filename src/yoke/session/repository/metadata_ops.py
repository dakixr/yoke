"""Session metadata patch operations."""

from __future__ import annotations

from typing import Any

from yoke.session.repository.models import SessionRecord
from yoke.session.repository.utils import normalize_title
from yoke.session.repository.utils import timestamp
from yoke.session.repository.writer import append_session_metadata


class _MetadataOperations:
    def _patch_metadata(
        self: Any,
        session_id: str,
        values: dict[str, object],
        *,
        existing_record: SessionRecord | None = None,
        error_name: str = "session",
    ) -> SessionRecord:
        record = existing_record or self.load(session_id)
        if record.id != session_id:
            raise ValueError(
                f"Existing session record id does not match {error_name} id."
            )
        if record.created_at is None:
            raise ValueError(f"Session not found: {session_id}")
        updated = record.model_copy(update=values)
        path = self._session_path(session_id)
        if path.exists():
            append_session_metadata(path, values)
        else:
            self._write_session_record(updated)
        self._index_service.update_metadata(updated)
        return updated

    def set_pinned(
        self: Any,
        session_id: str,
        pinned: bool,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        return self._patch_metadata(
            session_id,
            {"pinned": pinned},
            existing_record=existing_record,
            error_name="pin",
        )

    def set_title(
        self: Any,
        session_id: str,
        title: str | None,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        return self._patch_metadata(
            session_id,
            {"title": normalize_title(title), "updated_at": timestamp()},
            existing_record=existing_record,
            error_name="title",
        )

    def set_archived(
        self: Any,
        session_id: str,
        archived: bool,
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        now = timestamp()
        return self._patch_metadata(
            session_id,
            {"archived_at": now if archived else None, "updated_at": now},
            existing_record=existing_record,
            error_name="archive",
        )

    def set_selection(
        self: Any,
        session_id: str,
        *,
        provider_name: str | None,
        model_id: str | None,
        reasoning_effort: str | None,
        context_window_tokens: int | None,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        return self._patch_metadata(
            session_id,
            {
                "provider_name": provider_name,
                "model_id": model_id,
                "reasoning_effort": reasoning_effort,
                "context_window_tokens": context_window_tokens,
                "context_usage": None,
                "updated_at": timestamp(),
            },
            existing_record=existing_record,
            error_name="selection",
        )

    def set_context_usage(
        self: Any,
        session_id: str,
        usage: dict[str, object],
        *,
        existing_record: SessionRecord | None = None,
    ) -> SessionRecord:
        record = existing_record or self.summary_record(session_id)
        return self._patch_metadata(
            session_id,
            {"context_usage": dict(usage)},
            existing_record=record,
        )
