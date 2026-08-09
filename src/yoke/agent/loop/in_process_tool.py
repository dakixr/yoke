"""Cancellation supervision for tools that must run in the parent process."""

from __future__ import annotations

import queue
import threading
import time

from yoke.agent.loop.tool_core import cancelled_tool_result
from yoke.agent.loop.tool_core import execute_tool
from yoke.agent.loop.types import StopRequested
from yoke.agent.tools import LocalTool

IN_PROCESS_TOOL_POLL_SECONDS = 0.005
IN_PROCESS_TOOL_SHUTDOWN_SECONDS = 1.0
_WORKERS_LOCK = threading.Lock()
_ACTIVE_WORKERS: dict[int, dict[threading.Thread, threading.Event]] = {}
_SHUTTING_DOWN_TOOL_SETS: set[int] = set()


class InProcessToolShutdownError(RuntimeError):
    """Raised when an in-process tool outlives its bounded shutdown period."""


def execute_in_process_tool(
    *,
    tools: dict[str, LocalTool],
    name: str,
    arguments: dict[str, object],
    stop_requested: StopRequested | None,
) -> tuple[dict[str, object], bool]:
    """Run a parent-process tool and supervise cooperative cancellation."""
    result_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    cancel_event = threading.Event()

    def cancellation_requested() -> bool:
        return cancel_event.is_set() or (
            stop_requested is not None and stop_requested()
        )

    def run() -> None:
        try:
            result = execute_tool(
                tools,
                name,
                arguments,
                cancel_requested=cancellation_requested,
            )
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                pass
        finally:
            with _WORKERS_LOCK:
                workers = _ACTIVE_WORKERS.get(id(tools))
                if workers is not None:
                    workers.pop(threading.current_thread(), None)
                    if not workers:
                        _ACTIVE_WORKERS.pop(id(tools), None)

    worker = threading.Thread(target=run, daemon=True, name=f"yoke-tool-{name}")
    with _WORKERS_LOCK:
        if id(tools) in _SHUTTING_DOWN_TOOL_SETS:
            raise InProcessToolShutdownError(
                "Cannot start an in-process tool while its runtime is closing."
            )
        _ACTIVE_WORKERS.setdefault(id(tools), {})[worker] = cancel_event
    worker.start()
    while True:
        try:
            return result_queue.get(timeout=IN_PROCESS_TOOL_POLL_SECONDS), False
        except queue.Empty:
            if stop_requested is not None and stop_requested():
                # Latch cancellation for the worker. The caller's callback may be
                # transient, while the invocation must remain cancelled forever.
                cancel_event.set()
                return cancelled_tool_result(), True


def shutdown_in_process_tools(
    tools: dict[str, LocalTool],
    *,
    timeout_seconds: float | None = None,
) -> None:
    """Cancel workers and guarantee bounded, explicit runtime shutdown."""
    timeout = (
        IN_PROCESS_TOOL_SHUTDOWN_SECONDS
        if timeout_seconds is None
        else max(0.0, timeout_seconds)
    )
    tool_set_id = id(tools)
    with _WORKERS_LOCK:
        _SHUTTING_DOWN_TOOL_SETS.add(tool_set_id)
        workers = tuple(_ACTIVE_WORKERS.get(tool_set_id, {}).items())
        for _, cancel_event in workers:
            cancel_event.set()

    deadline = time.monotonic() + timeout
    for worker, _ in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))

    alive = [worker for worker, _ in workers if worker.is_alive()]
    if alive:
        names = ", ".join(sorted(worker.name for worker in alive))
        raise InProcessToolShutdownError(
            f"Runtime shutdown timed out after {timeout:g}s waiting for "
            f"{len(alive)} in-process tool worker(s): {names}. "
            "The runtime remains open; release the tools and close it again."
        )

    with _WORKERS_LOCK:
        _SHUTTING_DOWN_TOOL_SETS.discard(tool_set_id)


def wait_for_in_process_tools(tools: dict[str, LocalTool]) -> None:
    """Wait for detached workers in a background retirement path."""
    while True:
        with _WORKERS_LOCK:
            workers = tuple(_ACTIVE_WORKERS.get(id(tools), ()))
        if not workers:
            return
        for worker in workers:
            worker.join()
