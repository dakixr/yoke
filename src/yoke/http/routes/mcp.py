"""MCP configuration inspection and session-policy routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.errors import ApiError
from yoke.http.models.mcp import McpListResponse
from yoke.http.models.mcp import McpSessionPatchRequest
from yoke.http.models.mcp import McpSessionPatchResponse
from yoke.http.services.mcp_service import McpService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry


router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/mcp", response_model=McpListResponse, operation_id="listMcpServers")
def list_mcp_servers(
    request: Request,
    directory: str | None = Query(default=None),
    include_tools: bool = Query(default=False, alias="includeTools"),
) -> McpListResponse:
    service: McpService = request.app.state.mcp_service
    return service.list(
        directory=directory,
        session_id=None,
        include_tools=include_tools,
    )


@router.get(
    "/session/{session_id}/mcp",
    response_model=McpListResponse,
    operation_id="listSessionMcpServers",
)
def list_session_mcp_servers(
    request: Request,
    session_id: str,
    include_tools: bool = Query(default=False, alias="includeTools"),
) -> McpListResponse:
    service: McpService = request.app.state.mcp_service
    return service.list(
        directory=None,
        session_id=session_id,
        include_tools=include_tools,
    )


@router.patch(
    "/session/{session_id}/mcp/{server_name}",
    response_model=McpSessionPatchResponse,
    operation_id="patchSessionMcpServer",
)
async def patch_session_mcp_server(
    request: Request,
    session_id: str,
    server_name: str,
    body: McpSessionPatchRequest,
) -> McpSessionPatchResponse:
    service: McpService = request.app.state.mcp_service
    service.configured_server(session_id, server_name)
    runtimes: SessionRuntimeRegistry = request.app.state.runtime_registry
    updated_fields = body.model_fields_set & {
        "enabled",
        "enabled_tools",
        "disabled_tools",
    }
    if not updated_fields:
        raise ApiError(400, "empty_mutation", "At least one MCP field is required.")
    enabled_tools = (
        tuple(body.enabled_tools) if body.enabled_tools is not None else None
    )
    disabled_tools = (
        tuple(body.disabled_tools) if body.disabled_tools is not None else None
    )
    update_enabled_tools = "enabled_tools" in body.model_fields_set
    update_disabled_tools = "disabled_tools" in body.model_fields_set
    if body.scope == "session":
        await runtimes.set_mcp_policy(
            session_id,
            server_name,
            enabled=body.enabled,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
            update_enabled_tools=update_enabled_tools,
            update_disabled_tools=update_disabled_tools,
        )
    else:
        await runtimes.idle_mutation(
            session_id,
            lambda: service.patch_persisted(
                session_id,
                server_name,
                scope=body.scope,
                enabled=body.enabled,
                enabled_tools=enabled_tools,
                disabled_tools=disabled_tools,
                update_enabled_tools=update_enabled_tools,
                update_disabled_tools=update_disabled_tools,
            ),
        )
    return McpSessionPatchResponse(
        data=service.configured_server(session_id, server_name)
    )
