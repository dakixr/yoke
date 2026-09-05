"""Managed command, Python, process I/O, and environment tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.config import load_login_shell_environment
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


def test_child_environment_inherits_user_env_but_excludes_mcp_settings(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_MCP_BEARER_TOKEN", "do-not-leak")
    monkeypatch.setenv("YOKE_MCP_ROOT", "/private/server/root")
    monkeypatch.setenv("CODEX_LB_API_KEY", "load-balancer-key")
    monkeypatch.setenv("YOKE_CODEX_API_KEY", "codex-provider-key")
    monkeypatch.setenv("UNRELATED_USER_VALUE", "inherited")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": (
                            "import os; print(os.getenv('YOKE_MCP_BEARER_TOKEN')); "
                            "print(os.getenv('YOKE_MCP_ROOT')); "
                            "print(os.getenv('CODEX_LB_API_KEY')); "
                            "print(os.getenv('YOKE_CODEX_API_KEY')); "
                            "print(os.getenv('UNRELATED_USER_VALUE'))"
                        )
                    },
                )
            )
            assert result["output"].splitlines() == [
                "None",
                "None",
                "load-balancer-key",
                "codex-provider-key",
                "inherited",
            ]

    asyncio.run(scenario())


def test_login_shell_environment_preserves_mcp_settings(monkeypatch) -> None:
    monkeypatch.setenv("YOKE_MCP_BEARER_TOKEN", "service-secret")
    monkeypatch.setenv("PATH", "/service/bin")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                b"startup noise\n__YOKE_LOGIN_ENV_START__\0"
                b"PATH=/home/test/bin:/usr/bin\0"
                b"CODEX_LB_API_KEY=load-balancer-key\0"
                b"YOKE_MCP_BEARER_TOKEN=must-not-override\0"
            ),
            stderr=b"",
        ),
    )

    load_login_shell_environment()

    assert os.environ["PATH"] == "/home/test/bin:/usr/bin"
    assert os.environ["CODEX_LB_API_KEY"] == "load-balancer-key"
    assert os.environ["YOKE_MCP_BEARER_TOKEN"] == "service-secret"


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
                assert started["continue"] is True
                assert started["next_tool"] == "process_read"
                assert started["recommended_wait_ms"] == 240_000
                session_id = started["session_id"]
                final = structured(
                    await second.call_tool(
                        "process_io",
                        {"session_id": session_id, "chars": "", "yield_time_ms": 2_000},
                    )
                )
                assert final["running"] is False
                assert final["continue"] is False
                assert final["exit_code"] == 0
                assert "finished" in final["output"]

    asyncio.run(scenario())


def test_remote_wait_arguments_are_capped_before_execution(tmp_path: Path) -> None:
    service = create_service(MCPServerConfig(root=tmp_path, max_remote_wait_ms=5_000))

    assert (
        service.adapter._with_runtime_defaults(
            "exec_command", {"yield_time_ms": 999_999}
        )["yield_time_ms"]
        == 5_000
    )
    assert (
        service.adapter._with_runtime_defaults(
            "process_io", {"yield_time_ms": 999_999}
        )["yield_time_ms"]
        == 5_000
    )
    assert (
        service.adapter.execution._limit_remote_wait(
            "exec_python", {"yield_time_ms": 999_999}
        )["yield_time_ms"]
        == 5_000
    )
    assert (
        service.adapter.execution._limit_remote_wait(
            "process_read", {"wait_ms": 999_999}
        )["wait_ms"]
        == 5_000
    )


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
