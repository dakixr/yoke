"""Interruptible supervision for tools that require parent-process resources."""

from __future__ import annotations

import queue
import threading

from yoke.agent.loop.tools.core import cancelled_tool_result
from yoke.agent.loop.tools.core import execute_tool
from yoke.agent.loop.types import StopRequested
from yoke.agent.tools import LocalTool

IN_PROCESS_TOOL_POLL_SECONDS = 0.005
_WORKERS_LOCK = threading.Lock()
_ACTIVE_WORKERS: dict[int, set[threading.Thread]] = {}


def execute_in_process_tool(
    *,
    tools: dict[str, LocalTool],
    name: str,
    arguments: dict[str, object],
    stop_requested: StopRequested | None,
    tool_event,
) -> tuple[dict[str, object], bool]:
    """Run an in-process tool without letting it block cancellation handoff."""
    result_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result = execute_tool(
                tools,
                name,
                arguments,
                cancel_requested=stop_requested,
                tool_event=tool_event,
            )
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                pass
        finally:
            with _WORKERS_LOCK:
                workers = _ACTIVE_WORKERS.get(id(tools))
                if workers is not None:
                    workers.discard(threading.current_thread())
                    if not workers:
                        _ACTIVE_WORKERS.pop(id(tools), None)

    worker = threading.Thread(target=run, daemon=True, name=f"yoke-tool-{name}")
    with _WORKERS_LOCK:
        _ACTIVE_WORKERS.setdefault(id(tools), set()).add(worker)
    worker.start()
    while True:
        try:
            return result_queue.get(timeout=IN_PROCESS_TOOL_POLL_SECONDS), False
        except queue.Empty:
            if stop_requested is not None and stop_requested():
                return cancelled_tool_result(), True


def wait_for_in_process_tools(tools: dict[str, LocalTool]) -> None:
    """Wait off the control path until detached users of a tool set finish."""
    while True:
        with _WORKERS_LOCK:
            workers = tuple(_ACTIVE_WORKERS.get(id(tools), ()))
        if not workers:
            return
        for worker in workers:
            worker.join()
