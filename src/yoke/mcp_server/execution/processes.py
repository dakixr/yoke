"""Async observation of the shared, non-consuming process histories."""

from __future__ import annotations

import asyncio
from functools import partial
import threading
from typing import Any

from anyio import CapacityLimiter
from anyio.to_thread import run_sync
from pydantic import ConfigDict, Field

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.processes.models import ProcessCursor, ProcessReadRequest
from yoke.agent.tools.processes.observation import page, read_processes
from yoke.mcp_server.config import MAX_SAFE_REMOTE_WAIT_MS
from yoke.mcp_server.execution.models import Request
from yoke.mcp_server.execution.workers import admission, cancelled_result, settle

__all__ = ["ProcessCancel", "ProcessCursor", "ProcessRead", "page", "read"]


class ProcessRead(ProcessReadRequest):
    model_config = ConfigDict(extra="forbid")
    wait_ms: int = Field(
        default=60_000,
        ge=0,
        le=MAX_SAFE_REMOTE_WAIT_MS,
        strict=True,
        description=(
            "Maximum observation wait, capped by the server. Zero takes a snapshot. "
            "Expiry never stops a process."
        ),
    )


class ProcessCancel(Request):
    session_id: int = Field(ge=1, strict=True)


async def read(
    manager: CommandProcessManager,
    request: ProcessRead,
    *,
    slots: asyncio.Semaphore,
    limiter: CapacityLimiter,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Interrupt a cancelled waiting worker without terminating its processes."""
    interrupted = threading.Event()

    def cancelled() -> bool:
        return interrupted.is_set() or (cancel is not None and cancel.is_set())

    async def observe() -> dict[str, Any]:
        async with admission(slots, cancelled) as admitted:
            if not admitted:
                return {**cancelled_result(), "items": []}
            return await run_sync(
                partial(read_processes, manager, request, cancel_requested=cancelled),
                limiter=limiter,
            )

    return await settle(observe, interrupted)
