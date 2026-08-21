"""Real ASGI Streamable HTTP, health, and bearer protection tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import http_client


def test_health_is_public_but_mcp_requires_configured_bearer(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(root=tmp_path, bearer_token="test-secret")
        )
        transport = httpx2.ASGITransport(app=service.app)
        async with service.server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                headers={"Host": "localhost"},
            ) as client:
                health = await client.get("/healthz")
                denied = await client.post("/mcp", json={})
                assert health.status_code == 200
                assert health.json() == {"ok": True}
                assert denied.status_code == 401
                assert denied.headers["www-authenticate"] == "Bearer"
            async with http_client(service, token="test-secret") as mcp:
                result = await mcp.list_tools()
                assert len(result.tools) == 8

    asyncio.run(scenario())


def test_transport_rejects_unapproved_host_header(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        transport = httpx2.ASGITransport(app=service.app)
        async with service.server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://attacker.example",
                headers={"Host": "attacker.example"},
            ) as client:
                response = await client.post("/mcp", json={})
                assert response.status_code == 421

    asyncio.run(scenario())
