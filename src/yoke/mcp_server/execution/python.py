"""Initial Python observation and ownership of its bridge capability."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from yoke.agent.tools.python_exec import PythonExecTool
from yoke.mcp_server.execution.bridge import ComposePython
from yoke.mcp_server.execution.workers import admission, cancelled_result, settle

if TYPE_CHECKING:
    from yoke.mcp_server.execution.service import ExecutionService


async def execute(
    service: ExecutionService,
    request: ComposePython,
    *,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    interrupted = threading.Event()

    def cancelled() -> bool:
        return interrupted.is_set() or (cancel is not None and cancel.is_set())

    async def initial() -> dict[str, Any]:
        async with admission(service._orchestrations, cancelled) as admitted:
            if not admitted:
                return cancelled_result()
            token, run, code = await service.bridge.prepare(request)
            try:
                tool = PythonExecTool.bind(
                    root=service.config.root,
                    command_process_manager=service.runtime.manager,
                    cancel_requested=lambda: cancelled() or run.cancelled.is_set(),
                    default_exec_wait_ms=min(
                        service.config.default_yield_ms,
                        service.config.max_remote_wait_ms,
                    ),
                ).parse_arguments(
                    {
                        **request.model_dump(exclude={"managed_calls", "max_calls"}),
                        "code": code,
                    }
                )
                # Children need general operation slots while Python observes.
                result = await asyncio.to_thread(tool.execute)
            except BaseException:
                service.bridge.revoke(token)
                raise
            session = result.get("session_id")
            if isinstance(session, int) and result.get("running"):
                service._sessions[session] = token
                watcher = asyncio.create_task(
                    service._watch(session, token, run.deadline)
                )
                service._watchers.add(watcher)
                watcher.add_done_callback(service._watchers.discard)
            else:
                service.bridge.revoke(token)
            return result

    # Join the whole initial call, including capability adoption. Cancellation
    # stops observation, not the live process or its authorized bridge calls.
    return await settle(initial, interrupted)
