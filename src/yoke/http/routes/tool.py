"""Tool inventory and session-only enablement routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.tool import ToolListResponse
from yoke.http.models.tool import ToolPatchData
from yoke.http.models.tool import ToolPatchRequest
from yoke.http.models.tool import ToolPatchResponse
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.http.services.tool_service import ToolService


router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/tool", response_model=ToolListResponse, operation_id="listTools")
def list_tools(
    request: Request,
    directory: str | None = Query(default=None),
    session_id: str | None = Query(default=None, alias="sessionID"),
) -> ToolListResponse:
    service: ToolService = request.app.state.tool_service
    return service.inventory(directory=directory, session_id=session_id)


@router.patch(
    "/session/{session_id}/tool",
    response_model=ToolPatchResponse,
    operation_id="patchSessionTools",
)
async def patch_session_tools(
    request: Request,
    session_id: str,
    body: ToolPatchRequest,
) -> ToolPatchResponse:
    service: ToolService = request.app.state.tool_service
    discovered, defaults = service.discovered_names(session_id)
    runtimes: SessionRuntimeRegistry = request.app.state.runtime_registry
    enabled = await runtimes.set_tools(
        session_id,
        discovered_names=discovered,
        default_enabled_names=defaults,
        enabled=set(body.enabled),
        disabled=set(body.disabled),
    )
    return ToolPatchResponse(data=ToolPatchData(enabled=sorted(enabled)))
