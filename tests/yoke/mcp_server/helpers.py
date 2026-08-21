"""MCP client helpers used by server contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from typing import cast

import httpx2
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from yoke.mcp_server.server import MCPService


@asynccontextmanager
async def memory_client(service: MCPService) -> AsyncIterator[ClientSession]:
    """Connect one initialized SDK client without network transport."""
    async with InMemoryTransport(service.server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()
            yield client


@asynccontextmanager
async def http_client(
    service: MCPService,
    *,
    token: str | None = None,
) -> AsyncIterator[ClientSession]:
    """Connect one initialized SDK client through the real ASGI transport."""
    headers = {"Host": "localhost"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx2.ASGITransport(app=service.app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://localhost",
        headers=headers,
    ) as http:
        async with streamable_http_client(
            "http://localhost/mcp", http_client=http
        ) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                yield client


def structured(result: object) -> dict[str, Any]:
    """Return structured content from a complete tool result."""
    complete = cast(CallToolResult, result)
    return cast(dict[str, Any], complete.structured_content)
