"""Tool descriptor defaults and output schemas."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client


def test_descriptors_defaults_file_parameters_and_output_schemas(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, default_yield_ms=1234))
        async with memory_client(service) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert (
                tools["command_exec"].input_schema["properties"]["login"]["default"]
                is False
            )
            assert "wait_ms" not in tools["python_exec"].input_schema["properties"]
            assert tools["import_files"].meta == {"openai/fileParams": ["files"]}
            assert tools["batch_read"].output_schema
            assert tools["process_read"].annotations is not None
            assert tools["python_exec"].annotations is not None
            assert tools["process_read"].annotations.read_only_hint
            assert not tools["python_exec"].annotations.read_only_hint

    asyncio.run(scenario())
