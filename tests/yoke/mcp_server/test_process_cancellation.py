"""MCP wait cancellation and Python capability lifecycle regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time

import pytest

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.execution import processes
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def test_cancelled_read_joins_its_worker_without_terminating_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, stopped = threading.Event(), threading.Event()

    def waiting(manager, request, *, cancel_requested):
        started.set()
        while not cancel_requested():
            time.sleep(0.001)
        stopped.set()
        return {"ok": False, "reason": "cancelled", "items": []}

    monkeypatch.setattr(processes, "read_processes", waiting)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            task = asyncio.create_task(
                service.adapter.execution.dispatch(
                    "process_read",
                    {"sessions": [{"session_id": 123}], "wait_ms": 240_000},
                )
            )
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert stopped.is_set()
            assert not service.runtime.manager.snapshots()

    asyncio.run(scenario())


def test_cancelled_wait_leaves_observed_process_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "python_exec", {"code": "input()", "mode": "background"}
                )
            )
            task = asyncio.create_task(
                service.adapter.execution.dispatch(
                    "process_read",
                    {
                        "sessions": [
                            {
                                "session_id": running["session_id"],
                                "cursor": running["cursor"],
                            }
                        ],
                        "wait_ms": 240_000,
                    },
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            async with asyncio.timeout(2):
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert (
                service.runtime.manager.snapshot(running["session_id"]).status
                == "running"
            )
            assert service.adapter.execution.bridge._runs
            await client.call_tool(
                "process_cancel", {"session_id": running["session_id"]}
            )
            assert not service.adapter.execution.bridge._runs

    asyncio.run(scenario())


def test_cancelled_before_input_does_not_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "python_exec", {"code": "print(input())", "mode": "background"}
                )
            )
            cancelled = threading.Event()
            cancelled.set()
            result = await service.adapter.execution.local(
                "process_input",
                {"session_id": running["session_id"], "chars": "must-not-write\n"},
                cancel=cancelled,
            )
            assert not result["ok"]
            assert result["reason"] == "cancelled"
            result = structured(
                await client.call_tool(
                    "process_input",
                    {
                        "session_id": running["session_id"],
                        "chars": "kept\n",
                        "wait_ms": 5000,
                    },
                )
            )
            assert result["output"] == "kept\n"

    asyncio.run(scenario())


def test_explicit_cancel_revokes_authorized_managed_child(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        child_started, child_stopped = asyncio.Event(), asyncio.Event()
        cancellation = []

        async def managed(name, arguments, *, cancel=None):
            assert name == "mcp_call"
            assert arguments["arguments"] == {"value": "authorized"}
            child_started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancellation.append(cancel is not None and cancel.is_set())
                child_stopped.set()
            return {"ok": True}

        service.adapter.execution.bridge.dispatch = managed
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "python_exec",
                    {
                        "code": (
                            "import asyncio\nfrom yoke_mcp import tools\n"
                            "asyncio.run(tools.mcp('fixture', 'write', "
                            "{'value': 'authorized'}, schema_hash='pinned'))\n"
                        ),
                        "mode": "background",
                        "managed_calls": [
                            {
                                "server": "fixture",
                                "tool": "write",
                                "arguments": {"value": "authorized"},
                                "schema_hash": "pinned",
                            }
                        ],
                    },
                )
            )
            async with asyncio.timeout(5):
                await child_started.wait()
            assert service.adapter.execution.bridge._runs
            cancelled = structured(
                await client.call_tool(
                    "process_cancel", {"session_id": running["session_id"]}
                )
            )
            assert cancelled["ok"]
            async with asyncio.timeout(2):
                await child_stopped.wait()
            assert cancellation == [True]
            assert not service.adapter.execution.bridge._runs
            assert running["session_id"] not in service.adapter.execution._sessions
            assert (
                service.runtime.manager.snapshot(running["session_id"]).status
                != "running"
            )

    asyncio.run(scenario())


def test_cancelling_input_queued_behind_another_writer_prevents_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "python_exec", {"code": "print(input())", "mode": "background"}
                )
            )
            managed = service.runtime.manager._get(running["session_id"])
            managed.input_lock.acquire()
            try:
                task = asyncio.create_task(
                    service.adapter.call_tool(
                        "process_input",
                        {
                            "session_id": running["session_id"],
                            "chars": "must-not-write\n",
                        },
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                async with asyncio.timeout(2):
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                managed.input_lock.release()
            result = structured(
                await client.call_tool(
                    "process_input",
                    {
                        "session_id": running["session_id"],
                        "chars": "kept\n",
                        "wait_ms": 5000,
                    },
                )
            )
            assert result["output"] == "kept\n"

    asyncio.run(scenario())
