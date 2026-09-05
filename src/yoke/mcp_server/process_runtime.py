"""Shared ephemeral subprocess runtime for all MCP clients."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from anyio.to_thread import run_sync

from yoke.agent.tools.base import LocalTool
from yoke.mcp_server.execution.process_manager import MCPProcessManager


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
        self._patch_lock = asyncio.Lock()
        self._process_locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def execute(self, name: str, tool: LocalTool) -> dict[str, object]:
        """Run one parsed tool with narrow runtime coordination."""
        async with self._total:
            if name in {"exec_command", "exec_python"}:
                async with self._process_starts:
                    result = await self._run_sync(tool)
                session_id = result.get("session_id")
                if isinstance(session_id, int):
                    await self._process_lock(session_id)
                return result
            if name == "process_io":
                return await self._run_process_io(tool)
            if name == "apply_patch":
                async with self._patch_lock:
                    return await self._run_sync(tool)
            return await self._run_sync(tool)

    async def close(self) -> None:
        """Terminate live children and discard all ephemeral coordination state."""
        async with self._locks_guard:
            self._process_locks.clear()
        await run_sync(self.manager.close)

    async def _run_process_io(self, tool: LocalTool) -> dict[str, object]:
        raw_session_id = getattr(tool, "session_id", None)
        if not isinstance(raw_session_id, int):
            return {"ok": False, "error": "process_io requires a session_id"}
        lock = await self._process_lock(raw_session_id)
        async with lock:
            result = await self._run_sync(tool)
            if not result.get("running", False):
                async with self._locks_guard:
                    self._process_locks.pop(raw_session_id, None)
            return result

    async def _process_lock(self, session_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            return self._process_locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    async def _run_sync(tool: LocalTool) -> dict[str, object]:
        return await run_sync(tool.execute)
