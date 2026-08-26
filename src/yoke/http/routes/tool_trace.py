"""Tool inspector routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.tool_trace import ToolCallListResponse
from yoke.http.models.tool_trace import ToolCallResponse
from yoke.http.models.tool_trace import ToolOutputResponse
from yoke.http.services.tool_trace_service import ToolTraceService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> ToolTraceService:
    return request.app.state.tool_trace_service


@router.get(
    "/session/{session_id}/tool-call",
    response_model=ToolCallListResponse,
    operation_id="listSessionToolCalls",
)
def list_tool_calls(
    request: Request,
    session_id: str,
    status: Literal["pending", "running", "ok", "failed", "cancelled"] | None = Query(default=None),
    turn_id: int | None = Query(default=None, alias="turnID"),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> ToolCallListResponse:
    return _service(request).list_calls(
        session_id,
        status=status,
        turn_id=turn_id,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/session/{session_id}/tool-call/{call_id}",
    response_model=ToolCallResponse,
    operation_id="getSessionToolCall",
)
def get_tool_call(request: Request, session_id: str, call_id: str) -> ToolCallResponse:
    return _service(request).call(session_id, call_id)


@router.get(
    "/session/{session_id}/tool-call/{call_id}/output",
    response_model=ToolOutputResponse,
    operation_id="getSessionToolCallOutput",
)
def get_tool_output(
    request: Request,
    session_id: str,
    call_id: str,
    after_seq: int = Query(default=0, alias="afterSeq", ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> ToolOutputResponse:
    return _service(request).output(
        session_id,
        call_id,
        after_seq=after_seq,
        limit=limit,
    )
