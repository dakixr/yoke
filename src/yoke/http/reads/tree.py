"""Bounded tree pages and navigation previews over the session read model."""

from __future__ import annotations

from yoke.agent.models import ConversationEntry
from yoke.http.models.common import CursorInfo
from yoke.http.models.session import (
    TreeData,
    TreeNavigationAbandonedEntry,
    TreeNavigationPreviewData,
    TreeNavigationPreviewResponse,
    TreeResponse,
)
from yoke.http.reads.cursor import decode_cursor, encode_cursor, query_fingerprint
from yoke.http.reads.cursors import revision_from_timestamp
from yoke.http.reads.model import SessionReadModel
from yoke.http.services.event_broker import EventService
from yoke.http.services.projectors import project_tree_entry


class SessionTreeReads:
    """Project bounded tree pages and navigation previews."""

    def __init__(
        self, read_model: SessionReadModel, events: EventService | None
    ) -> None:
        self.read_model = read_model
        self.events = events

    def tree(
        self,
        session_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> TreeResponse:
        fingerprint = query_fingerprint((session_id, "tree"))
        anchor = (
            decode_cursor(cursor, expected_query=fingerprint)
            if cursor is not None
            else None
        )
        indexed = self.read_model.tree_page(
            session_id,
            limit=limit,
            anchor_id=anchor,
        )
        next_cursor = (
            encode_cursor(
                query=fingerprint,
                anchor_id=indexed.entries[0].id,
            )
            if indexed.has_more and indexed.entries
            else None
        )
        summary = self.read_model.summary(session_id)
        return TreeResponse(
            data=TreeData(
                revision=revision_from_timestamp(
                    summary.updated_at if summary is not None else None
                ),
                leaf_id=indexed.leaf_id,
                entries=[
                    project_tree_entry(
                        entry,
                        leaf_id=indexed.leaf_id,
                        active=entry.id in indexed.active_ids,
                        child_count=indexed.child_counts.get(entry.id, 0),
                        kind_override="tool_context"
                        if entry.id in indexed.legacy_tool_context_ids
                        else None,
                    )
                    for entry in indexed.entries
                ],
                total_entries=indexed.total_entries,
                cursor=CursorInfo(previous=None, next=next_cursor),
            )
        )

    def navigation_preview(
        self,
        session_id: str,
        *,
        target_id: str,
        include_abandoned: bool,
    ) -> TreeNavigationPreviewResponse:
        indexed = self.read_model.navigation_preview(
            session_id,
            target_id=target_id,
            include_abandoned=include_abandoned,
        )
        editor_text = (
            indexed.target.message.display_text_content() or ""
            if indexed.target.kind == "user" and indexed.target.message is not None
            else None
        )
        abandoned = [
            TreeNavigationAbandonedEntry(
                id=entry.id,
                kind=entry.kind,
                preview=_entry_preview_text(entry),
            )
            for entry in indexed.abandoned
        ]
        return TreeNavigationPreviewResponse(
            data=TreeNavigationPreviewData(
                target_id=target_id,
                current=indexed.current,
                editor_text=editor_text,
                abandoned=abandoned,
                abandoned_total=(indexed.abandoned_total if include_abandoned else 0),
                abandoned_truncated=(
                    indexed.abandoned_truncated if include_abandoned else False
                ),
            )
        )


def _preview_text(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _entry_preview_text(entry: ConversationEntry) -> str | None:
    if entry.message is not None:
        return _preview_text(entry.message.display_text_content())
    summary = entry.metadata.get("summary")
    return _preview_text(summary if isinstance(summary, str) else None)
