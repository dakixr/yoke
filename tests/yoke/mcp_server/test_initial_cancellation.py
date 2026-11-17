"""Queued orchestration cancellation prevents process creation."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading


from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client


def test_python_cancel_event_prevents_queued_orchestration_from_spawning(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            execution = service.adapter.execution
            execution._orchestrations = asyncio.Semaphore(1)
            await execution._orchestrations.acquire()
            cancel = threading.Event()
            try:
                task = asyncio.create_task(
                    execution.dispatch(
                        "python_exec",
                        {"code": "open('unexpected', 'w').close()"},
                        cancel=cancel,
                    )
                )
                await asyncio.sleep(0.05)
                assert not task.done()
                cancel.set()
                async with asyncio.timeout(2):
                    result = await task
                assert result["reason"] == "cancelled"
                assert not service.runtime.manager.snapshots()
                assert not execution.bridge._runs
            finally:
                execution._orchestrations.release()
            await asyncio.sleep(0.05)
            assert not (tmp_path / "unexpected").exists()
            assert not service.runtime.manager.snapshots()

    asyncio.run(scenario())
