"""Low-level MCP server and Streamable HTTP application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolRequestParams
from mcp.types import CallToolResult
from mcp.types import ListToolsResult
from mcp.types import PaginatedRequestParams
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from yoke._version import __version__
from yoke.mcp_server.adapter import ToolAdapter
from yoke.mcp_server.auth import BearerTokenMiddleware
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.process_runtime import ProcessRuntime


@dataclass(slots=True)
class MCPService:
    """Objects owned by one configured MCP application runtime."""

    config: MCPServerConfig
    server: Server[ProcessRuntime]
    runtime: ProcessRuntime
    app: ASGIApp


def create_service(config: MCPServerConfig) -> MCPService:
    """Construct one shared-runtime, multi-client Yoke MCP service."""
    runtime = ProcessRuntime(
        command_environment=config.command_environment(),
        max_concurrent_calls=config.max_concurrent_calls,
        max_concurrent_process_starts=config.max_concurrent_process_starts,
    )
    adapter = ToolAdapter(config, runtime)

    @asynccontextmanager
    async def lifespan(_: Server[ProcessRuntime]) -> AsyncIterator[ProcessRuntime]:
        try:
            yield runtime
        finally:
            await runtime.close()

    async def list_tools(_: Any, __: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=adapter.list_tools())

    async def call_tool(_: Any, params: CallToolRequestParams) -> CallToolResult:
        arguments = params.arguments if isinstance(params.arguments, dict) else {}
        return await adapter.call_tool(params.name, arguments)

    server: Server[ProcessRuntime] = Server(
        "yoke-server-harness",
        version=__version__,
        title="Yoke server harness",
        description="Tool-only remote coding and server execution harness.",
        instructions=(
            "Inspect before modifying. Use apply_patch for file changes and "
            "process_io for commands that return a live session ID."
        ),
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.transport_allowed_hosts,
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )
    starlette_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=config.max_request_body_size,
        transport_security=transport_security,
        host=config.host,
        custom_starlette_routes=[Route("/healthz", _health)],
    )
    app: ASGIApp = BearerTokenMiddleware(starlette_app, config.bearer_token)
    return MCPService(config=config, server=server, runtime=runtime, app=app)


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})
