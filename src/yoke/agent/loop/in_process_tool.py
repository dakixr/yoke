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


class InProcessToolInvocation:
    """One parent-process tool invocation with cooperative cancellation."""

    def __init__(
        self,
        *,
        tools: dict[str, LocalTool],
        name: str,
        arguments: dict[str, object],
    ) -> None:
        self._tools = tools
        self._name = name
        self._arguments = arguments
        self._result_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self._cancel_event = threading.Event()
        self._result: dict[str, object] | None = None
        self._started = False
        self._worker = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"yoke-tool-{name}",
        )

    def start(self) -> None:
        """Start the worker thread."""
        with _WORKERS_LOCK:
            if id(self._tools) in _SHUTTING_DOWN_TOOL_SETS:
                raise InProcessToolShutdownError(
                    "Cannot start an in-process tool while its runtime is closing."
                )
            _ACTIVE_WORKERS.setdefault(id(self._tools), {})[self._worker] = (
                self._cancel_event
            )
        try:
            self._worker.start()
        except BaseException:
            with _WORKERS_LOCK:
                workers = _ACTIVE_WORKERS.get(id(self._tools))
                if workers is not None:
                    workers.pop(self._worker, None)
                    if not workers:
                        _ACTIVE_WORKERS.pop(id(self._tools), None)
            raise
        self._started = True

    def done(self) -> bool:
        """Return whether the worker produced a final result."""
        if self._result is not None:
            return True
        try:
            self._result = self._result_queue.get_nowait()
            return True
        except queue.Empty:
            pass
        if not self._started or self._worker.is_alive():
            return False
        try:
            self._result = self._result_queue.get_nowait()
        except queue.Empty:
            self._result = {
                "ok": False,
                "error": "In-process tool finished without returning a result",
            }
        return True

    def result(self) -> dict[str, object]:
        """Return the final result, blocking until it is available."""
        while not self.done():
            time.sleep(IN_PROCESS_TOOL_POLL_SECONDS)
        assert self._result is not None
        return self._result

    def cancel(self) -> None:
        """Latch cooperative cancellation for the worker."""
        self._cancel_event.set()

    def _run(self) -> None:
        try:
            result = execute_tool(
                self._tools,
                self._name,
                self._arguments,
                cancel_requested=self._cancel_event.is_set,
            )
            try:
                self._result_queue.put_nowait(result)
            except queue.Full:
                pass
        finally:
            with _WORKERS_LOCK:
                workers = _ACTIVE_WORKERS.get(id(self._tools))
                if workers is not None:
                    workers.pop(threading.current_thread(), None)
                    if not workers:
                        _ACTIVE_WORKERS.pop(id(self._tools), None)


def execute_in_process_tool(
    *,
    tools: dict[str, LocalTool],
    name: str,
    arguments: dict[str, object],
    stop_requested: StopRequested | None,
) -> tuple[dict[str, object], bool]:
    """Run a parent-process tool and supervise cooperative cancellation."""
    invocation = InProcessToolInvocation(
        tools=tools,
        name=name,
        arguments=arguments,
    )
    invocation.start()
    while True:
        if invocation.done():
            return invocation.result(), False
        if stop_requested is not None and stop_requested():
            # Latch cancellation for the worker. The caller's callback may be
            # transient, while the invocation must remain cancelled forever.
            invocation.cancel()
            return cancelled_tool_result(), True
        time.sleep(IN_PROCESS_TOOL_POLL_SECONDS)


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
