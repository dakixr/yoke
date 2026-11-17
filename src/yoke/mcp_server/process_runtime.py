"""Shared ephemeral subprocess runtime for all MCP clients."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any
import threading

from anyio import CapacityLimiter
from anyio.to_thread import run_sync

from yoke.agent.tools.base import LocalTool
from yoke.mcp_server.execution import processes
from yoke.mcp_server.execution.processes import ProcessRead
from yoke.mcp_server.execution.process_manager import MCPProcessManager
from yoke.mcp_server.execution.workers import admission, cancelled_result, settle


class ProcessRuntime:
    """Coordinate tool calls around one service-global process manager."""

    def __init__(
        self,
        *,
        command_environment: Mapping[str, str],
        max_concurrent_calls: int,
        max_concurrent_process_starts: int,
    ) -> None:
        self.manager = MCPProcessManager(base_environment=command_environment)
        self._total = asyncio.Semaphore(max_concurrent_calls)
        self._process_starts = asyncio.Semaphore(max_concurrent_process_starts)
        self._controls = asyncio.Semaphore(max_concurrent_calls)
        self._control_threads = CapacityLimiter(max_concurrent_calls)
        self._reads = asyncio.Semaphore(max_concurrent_calls)
        self._read_threads = CapacityLimiter(max_concurrent_calls)
        self._patch_lock = asyncio.Lock()

    async def run_operation[T](self, operation: Callable[[], T]) -> T:
        """Run blocking service work under the shared call capacity limit."""
        async with self._total:
            return await run_sync(operation)

    async def read_process(
        self, request: ProcessRead, *, cancel: threading.Event | None = None
    ) -> dict[str, Any]:
        """Observe process history using capacity reserved for readers."""
        return await processes.read(
            self.manager,
            request,
            cancel=cancel,
            slots=self._reads,
            limiter=self._read_threads,
        )

    @asynccontextmanager
    async def patch_guard(self) -> AsyncIterator[None]:
        """Serialize patch recipes with individual apply_patch calls."""
        async with self._patch_lock:
            yield

    async def execute(
        self, name: str, tool: LocalTool, *, cancel: threading.Event
    ) -> dict[str, object]:
        """Run one parsed tool with narrow runtime coordination."""
        if name in {"command_exec", "python_exec", "process_input"}:
            return await settle(lambda: self._process(name, tool, cancel), cancel)
        if name == "process_cancel":
            async with self._controls:
                return await run_sync(tool.execute, limiter=self._control_threads)
        async with self._total:
            if name == "apply_patch":
                async with self._patch_lock:
                    return await self._run_sync(tool)
            return await self._run_sync(tool)

    async def _process(
        self, name: str, tool: LocalTool, cancel: threading.Event
    ) -> dict[str, object]:
        slots = self._controls if name == "process_input" else self._process_starts
        async with admission(slots, cancel.is_set) as admitted:
            if not admitted:
                return cancelled_result()
            if name == "process_input":
                # Control cannot queue behind initial waits in either thread pool.
                return await run_sync(tool.execute, limiter=self._control_threads)
            # Queued starts must not reserve general operation capacity.
            async with admission(self._total, cancel.is_set) as admitted:
                if not admitted:
                    return cancelled_result()
                return await asyncio.to_thread(tool.execute)

    async def close(self) -> None:
        """Terminate live children and discard all ephemeral coordination state."""
        await run_sync(self.manager.close)

    @staticmethod
    async def _run_sync(tool: LocalTool) -> dict[str, object]:
        return await run_sync(tool.execute)
