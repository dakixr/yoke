"""Select a canonical call window without changing the entries themselves."""

from collections.abc import Callable
from typing import Literal

from yoke.agent.observability import ToolTraceEntry
from yoke.http.errors import ApiError
from yoke.http.models.common import CursorInfo
from yoke.http.models.tool_trace import ToolCallInfo
from yoke.http.models.tool_trace import ToolCallListResponse
from yoke.http.services.cursor import decode_cursor
from yoke.http.services.cursor import encode_cursor
from yoke.http.services.cursor import query_fingerprint


def call_window(
    entries: list[ToolTraceEntry],
    *,
    session_id: str,
    status: str | None,
    turn_id: int | None,
    limit: int,
    cursor: str | None,
    order: Literal["oldest", "latest"],
    global_sequences: bool,
    project: Callable[[ToolTraceEntry, int | None], ToolCallInfo],
) -> ToolCallListResponse:
    """Page in either direction while retaining canonical row order and counts."""
    sequences = (
        {entry.tool_call_id: index for index, entry in enumerate(entries, start=1)}
        if global_sequences
        else {}
    )
    selected = [
        entry
        for entry in entries
        if (status is None or entry.status == status)
        and (turn_id is None or entry.turn_id == turn_id)
    ]
    # Preserve old cursor fingerprints for existing oldest-mode clients.
    query = (
        (session_id, status, turn_id, order)
        if order == "latest"
        else (
            session_id,
            status,
            turn_id,
        )
    )
    fingerprint = query_fingerprint(query)
    boundary = len(selected) if order == "latest" else 0
    if cursor is not None:
        anchor = decode_cursor(cursor, expected_query=fingerprint)
        for index, entry in enumerate(selected):
            if entry.tool_call_id == anchor:
                boundary = index if order == "latest" else index + 1
                break
        else:
            raise ApiError(
                400, "invalid_cursor_anchor", "Cursor anchor no longer exists."
            )
    start, end = (
        (max(0, boundary - limit), boundary)
        if order == "latest"
        else (boundary, boundary + limit)
    )
    page = selected[start:end]
    has_more = start > 0 if order == "latest" else end < len(selected)
    next_cursor = (
        encode_cursor(
            query=fingerprint,
            anchor_id=page[0 if order == "latest" else -1].tool_call_id,
        )
        if has_more and page
        else None
    )
    return ToolCallListResponse(
        data=[project(entry, sequences.get(entry.tool_call_id)) for entry in page],
        cursor=CursorInfo(previous=None, next=next_cursor),
        total=len(selected),
    )
