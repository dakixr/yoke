"""Low-level MCP server and Streamable HTTP application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolRequestParams
from mcp.types import CallToolResult
from mcp.types import ListToolsResult
from mcp.types import PaginatedRequestParams
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from yoke._version import __version__
from yoke.mcp_server.adapter import ToolAdapter
from yoke.mcp_server.auth import BearerTokenMiddleware
from yoke.mcp_server.auth import SingleUserOAuthProvider
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.process_runtime import ProcessRuntime


@dataclass(slots=True)
class MCPService:
    """Objects owned by one configured MCP application runtime."""

    config: MCPServerConfig
    server: Server[ProcessRuntime]
    runtime: ProcessRuntime
    app: ASGIApp
    oauth_provider: SingleUserOAuthProvider | None = None


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
    custom_routes = [Route("/healthz", _health)]
    oauth_provider = None
    resource_metadata_url = None
    if config.oauth_issuer_url and config.oauth_authorization_password:
        if config.oauth_state_file is None:  # pragma: no cover - validated by config
            raise RuntimeError("OAuth state file was not configured")
        issuer_url = AnyHttpUrl(config.oauth_issuer_url)
        resource_url = AnyHttpUrl(f"{config.oauth_issuer_url}/mcp")
        oauth_provider = SingleUserOAuthProvider(
            issuer_url=config.oauth_issuer_url,
            resource_url=str(resource_url),
            authorization_password=config.oauth_authorization_password,
            state_file=config.oauth_state_file,
            allowed_redirect_hosts=config.oauth_allowed_redirect_hosts,
        )
        custom_routes.extend(
            create_auth_routes(
                oauth_provider,
                issuer_url,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=["yoke"],
                    default_scopes=["yoke"],
                ),
            )
        )
        custom_routes.extend(
            create_protected_resource_routes(
                resource_url,
                [issuer_url],
                resource_name="Yoke Mooncake",
            )
        )
        custom_routes.extend(
            [
                Route("/oauth/consent", oauth_provider.consent_page, methods=["GET"]),
                Route(
                    "/oauth/consent",
                    oauth_provider.complete_consent,
                    methods=["POST"],
                ),
            ]
        )
        resource_metadata_url = str(build_resource_metadata_url(resource_url))

    starlette_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=config.max_request_body_size,
        transport_security=transport_security,
        host=config.host,
        custom_starlette_routes=custom_routes,
    )
    app: ASGIApp = BearerTokenMiddleware(
        starlette_app,
        config.bearer_token,
        oauth_provider=oauth_provider,
        resource_metadata_url=resource_metadata_url,
    )
    return MCPService(
        config=config,
        server=server,
        runtime=runtime,
        app=app,
        oauth_provider=oauth_provider,
    )


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})
