"""Public message and context projections over the session read model."""

from __future__ import annotations

from typing import Literal

from yoke.http.models.common import CursorInfo
from yoke.http.models.session import (
    ContextResponse,
    MessageListResponse,
    MessageResponse,
)
from yoke.http.services.cursor import decode_cursor, encode_cursor, query_fingerprint
from yoke.http.services.event_broker import EventService
from yoke.http.services.projectors import project_entry
from yoke.http.services.session_read_model import SessionReadModel

MessageOrder = Literal["asc", "desc"]


class SessionMessages:
    """Project public messages and bounded context through a single read model."""

    def __init__(
        self, read_model: SessionReadModel, events: EventService | None
    ) -> None:
        self.read_model = read_model
        self.events = events

    def messages(
        self,
        session_id: str,
        *,
        limit: int,
        order: MessageOrder,
        cursor: str | None,
    ) -> MessageListResponse:
        snapshot_seq = (
            self.events.journal.latest_sequence(session_id)
            if self.events is not None
            else 0
        )
        fingerprint = query_fingerprint((session_id, "active", order))
        anchor = (
            decode_cursor(cursor, expected_query=fingerprint)
            if cursor is not None
            else None
        )
        indexed_page = self.read_model.page(
            session_id,
            limit=limit,
            order=order,
            anchor_id=anchor,
        )
        page = [project_entry(entry) for entry in indexed_page.entries]
        next_cursor = (
            encode_cursor(query=fingerprint, anchor_id=page[-1].id)
            if indexed_page.has_more and page
            else None
        )
        return MessageListResponse(
            data=page,
            cursor=CursorInfo(previous=None, next=next_cursor),
            snapshot_seq=snapshot_seq,
        )

    def message(self, session_id: str, message_id: str) -> MessageResponse:
        return MessageResponse(
            data=project_entry(self.read_model.message(session_id, message_id))
        )

    def context(
        self,
        session_id: str,
        *,
        include_system: bool,
        include_tool_results: bool,
        limit: int,
        max_chars: int,
    ) -> ContextResponse:
        return self.read_model.context(
            session_id,
            include_system=include_system,
            include_tool_results=include_tool_results,
            limit=limit,
            max_chars=max_chars,
        )
