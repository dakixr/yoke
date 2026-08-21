"""Managed command, Python, process I/O, and environment tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import http_client
from .helpers import memory_client
from .helpers import structured


def test_quick_command_and_python_return_final_results(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            command = structured(
                await client.call_tool("exec_command", {"cmd": "printf hello"})
            )
            python = structured(
                await client.call_tool("exec_python", {"code": "print(6 * 7)"})
            )
            assert command["ok"] is True
            assert command["running"] is False
            assert command["output"] == "hello"
            assert python["ok"] is True
            assert python["running"] is False
            assert python["output"] == "42"

    asyncio.run(scenario())


def test_child_environment_excludes_service_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_MCP_BEARER_TOKEN", "do-not-leak")
    monkeypatch.setenv("UNRELATED_SERVICE_SECRET", "also-do-not-leak")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": (
                            "import os; print(os.getenv('YOKE_MCP_BEARER_TOKEN')); "
                            "print(os.getenv('UNRELATED_SERVICE_SECRET'))"
                        )
                    },
                )
            )
            assert result["output"].splitlines() == ["None", "None"]

    asyncio.run(scenario())


def test_process_handle_works_across_http_clients(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = MCPServerConfig(root=tmp_path, default_yield_ms=250)
        service = create_service(config)
        async with service.server.session_manager.run():
            async with http_client(service) as first, http_client(service) as second:
                started = structured(
                    await first.call_tool(
                        "exec_python",
                        {
                            "code": (
                                "import time; print('started', flush=True); "
                                "time.sleep(0.7); print('finished', flush=True)"
                            ),
                            "yield_time_ms": 250,
                        },
                    )
                )
                assert started["running"] is True
                session_id = started["session_id"]
                final = structured(
                    await second.call_tool(
                        "process_io",
                        {"session_id": session_id, "chars": "", "yield_time_ms": 2_000},
                    )
                )
                assert final["running"] is False
                assert final["exit_code"] == 0
                assert "finished" in final["output"]

    asyncio.run(scenario())


def test_python_timeout_kills_the_managed_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": "import time; time.sleep(10)",
                        "timeout": 1,
                        "yield_time_ms": 2_000,
                    },
                )
            )
            assert result["ok"] is False
            assert result["timed_out"] is True
            assert "timed out" in result["error"]

    asyncio.run(scenario())


def test_large_output_is_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, max_output_tokens=200))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool("exec_python", {"code": "print('x' * 100_000)"})
            )
            assert result["ok"] is True
            assert len(result["output"]) < 2_000
            assert result["original_token_count"] > 20_000

    asyncio.run(scenario())


def test_shutdown_terminates_live_processes(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, default_yield_ms=250))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_command",
                    {
                        "cmd": f"{sys.executable} -c 'import time; time.sleep(60)'",
                        "yield_time_ms": 250,
                    },
                )
            )
            assert result["running"] is True
            assert service.runtime.manager.snapshots()
        assert service.runtime.manager.snapshots() == []

    asyncio.run(scenario())
