"""Cancellation supervision for tools that must run in the parent process."""

from __future__ import annotations

import queue
import threading
import time

from yoke.agent.loop.tool_core import cancelled_tool_result, execute_tool
from yoke.agent.loop.types import StopRequested
from yoke.agent.tools import LocalTool
from yoke.agent_runs.context import (
    Binding,
    bind_binding,
    release_tool_binding,
    tool_binding,
)
from yoke.ai.providers.usage_context import (
    bind_usage_metric_context,
    current_usage_metric_context,
)

IN_PROCESS_TOOL_POLL_SECONDS = 0.005
IN_PROCESS_TOOL_SHUTDOWN_SECONDS = 1.0


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
        self._workers = workers_for_tools(tools)
        self._name = name
        self._arguments = arguments
        self._result_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self._cancel_event = threading.Event()
        self._result: dict[str, object] | None = None
        self._started = False
        self._usage_context = current_usage_metric_context()
        self._run_binding = tool_binding(tools)
        self._worker = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"yoke-tool-{name}",
        )

    def start(self) -> None:
        """Start the worker thread."""
        self._workers.start(self._worker, self._cancel_event, self._run_binding)
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
            with (
                bind_usage_metric_context(self._usage_context),
                bind_binding(self._run_binding),
            ):
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
            release_tool_binding(self._run_binding)
            self._workers.finished(threading.current_thread())


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
            if getattr(tools.get(name), "preserve_cancelled_result", False):
                # Process cancellation can leave running work or already-sent
                # input. Preserve the worker's handle, cursor, and delivery state.
                deadline = time.monotonic() + IN_PROCESS_TOOL_SHUTDOWN_SECONDS
                while time.monotonic() < deadline:
                    if invocation.done():
                        return invocation.result(), True
                    time.sleep(IN_PROCESS_TOOL_POLL_SECONDS)
                result = cancelled_tool_result()
                result.update(reason="cancelled", outcome="unknown")
                if name == "process_input":
                    result["input_written"] = None
                return result, True
            return cancelled_tool_result(), True
        time.sleep(IN_PROCESS_TOOL_POLL_SECONDS)


class InProcessTools:
    """Own cancellable workers for one installed runtime tool index."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[threading.Thread, threading.Event] = {}
        self._shutting_down = False

    def start(
        self, worker: threading.Thread, cancel: threading.Event, binding: Binding | None
    ) -> None:
        """Register a worker before starting it, excluding concurrent shutdown."""
        with self._lock:
            if self._shutting_down:
                release_tool_binding(binding)
                raise InProcessToolShutdownError(
                    "Cannot start an in-process tool while its runtime is closing."
                )
            self._workers[worker] = cancel
        try:
            worker.start()
        except BaseException:
            release_tool_binding(binding)
            self.finished(worker)
            raise

    def finished(self, worker: threading.Thread) -> None:
        """Forget a worker after its invocation releases its binding."""
        with self._lock:
            self._workers.pop(worker, None)

    def shutdown(self, *, timeout_seconds: float | None = None) -> None:
        """Cancel workers with the same bounded, retryable shutdown contract."""
        timeout = (
            IN_PROCESS_TOOL_SHUTDOWN_SECONDS
            if timeout_seconds is None
            else max(0.0, timeout_seconds)
        )
        with self._lock:
            self._shutting_down = True
            workers = tuple(self._workers.items())
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
        with self._lock:
            self._shutting_down = False

    def wait(self) -> None:
        """Wait for detached workers in a background retirement path."""
        while True:
            with self._lock:
                workers = tuple(self._workers)
            if not workers:
                return
            for worker in workers:
                worker.join()


class RuntimeTools(dict[str, LocalTool]):
    """The runtime's tool index, carrying its worker owner without a global registry."""

    def __init__(
        self, workers: InProcessTools, tools: dict[str, LocalTool] | None = None
    ) -> None:
        super().__init__(tools or {})
        self.workers = workers


def workers_for_tools(tools: dict[str, LocalTool]) -> InProcessTools:
    """Find runtime ownership, or attach an owner to standalone tool prototypes."""
    if isinstance(tools, RuntimeTools):
        return tools.workers
    for tool in tools.values():
        workers = tool.binding_value("in_process_workers")
        if isinstance(workers, InProcessTools):
            return workers
    workers = InProcessTools()
    for tool in tools.values():
        tool.bind_value("in_process_workers", workers)
    return workers


def shutdown_in_process_tools(
    tools: dict[str, LocalTool], *, timeout_seconds: float | None = None
) -> None:
    """Cancel workers owned by the runtime or standalone tool collection."""
    workers_for_tools(tools).shutdown(timeout_seconds=timeout_seconds)


def wait_for_in_process_tools(tools: dict[str, LocalTool]) -> None:
    """Wait for the collection's detached workers before retiring its resources."""
    workers_for_tools(tools).wait()
