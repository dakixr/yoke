"""Public process request policy and non-consuming observation contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("command_exec", {"argv": ["unused"]}),
        ("python_exec", {"code": "raise AssertionError('must not run')"}),
        ("process_input", {"session_id": 1, "chars": "x"}),
        ("process_read", {"sessions": [{"session_id": 1}]}),
        ("process_cancel", {"session_id": 1}),
    ],
)
def test_legacy_wait_field_is_rejected(
    tmp_path: Path, name: str, arguments: dict
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            result = structured(
                await service.adapter.call_tool(name, {**arguments, "yield_time_ms": 0})
            )
            assert result["error_code"] == "INVALID_ARGUMENT"
            assert result["execution_started"] is False
            assert not service.runtime.manager.snapshots()

    asyncio.run(scenario())
