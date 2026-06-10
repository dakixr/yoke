"""Process-isolated tool execution helpers."""

from __future__ import annotations

import multiprocessing
import os
import pickle
import queue
import signal
import time
from multiprocessing.context import BaseContext
from multiprocessing.context import Process
from multiprocessing.queues import Queue

from yoke.agent.loop.tool_core import cancelled_tool_result
from yoke.agent.loop.tool_core import execute_tool
from yoke.agent.loop.types import StopRequested
from yoke.agent.tools import LocalTool


TOOL_CANCEL_GRACE_SECONDS = 0.25
TOOL_POLL_SECONDS = 0.02


class ToolProcessInvocation:
    """A single tool invocation running in an isolated child process."""

    def __init__(
        self,
        *,
        tools: dict[str, LocalTool],
        name: str,
        arguments: dict[str, object],
    ) -> None:
        self._context = _process_context()
        self._cancel_event = self._context.Event()
        self._result_queue: Queue[dict[str, object]] = self._context.Queue(maxsize=1)
        self._process: Process = self._context.Process(
            target=_tool_process_main,
            args=(tools, name, arguments, self._cancel_event, self._result_queue),
        )
        self._result: dict[str, object] | None = None
        self._cancelled = False

    def start(self) -> None:
        """Start the child process."""
        self._process.start()

    def done(self) -> bool:
        """Return whether the invocation has produced a final result."""
        if self._result is not None:
            return True
        try:
            self._result = self._result_queue.get_nowait()
            return True
        except queue.Empty:
            pass
        if self._process.is_alive():
            return False
        self._process.join(timeout=0)
        try:
            self._result = self._result_queue.get_nowait()
        except queue.Empty:
            if self._cancelled:
                self._result = cancelled_tool_result()
            elif self._process.exitcode == 0:
                self._result = {
                    "ok": False,
                    "error": "Tool process exited without returning a result",
                }
            else:
                self._result = {
                    "ok": False,
                    "error": f"Tool process exited with status {self._process.exitcode}",
                }
        return True

    def result(self) -> dict[str, object]:
        """Return the final result, blocking until it is available."""
        while not self.done():
            time.sleep(TOOL_POLL_SECONDS)
        assert self._result is not None
        self.close()
        return self._result

    def cancel(self) -> None:
        """Request cancellation and terminate the child process if needed."""
        self._cancelled = True
        self._cancel_event.set()
        if not self._process.is_alive():
            self.close()
            return
        self._process.join(timeout=TOOL_CANCEL_GRACE_SECONDS)
        if not self._process.is_alive():
            self.close()
            return
        _terminate_process_group(self._process)
        self._process.join(timeout=TOOL_CANCEL_GRACE_SECONDS)
        if self._process.is_alive():
            _kill_process_group(self._process)
            self._process.join()
        self.close()

    def close(self) -> None:
        """Release multiprocessing resources."""
        if self._process.is_alive():
            return
        self._process.join(timeout=0)
        self._result_queue.close()


def wait_for_tool_process(
    invocation: ToolProcessInvocation,
    *,
    stop_requested: StopRequested | None,
) -> tuple[dict[str, object], bool]:
    """Wait for one tool process, cancelling it if the turn stops."""
    while not invocation.done():
        if stop_requested is not None and stop_requested():
            invocation.cancel()
            return cancelled_tool_result(), True
        time.sleep(TOOL_POLL_SECONDS)
    return invocation.result(), False


def _tool_process_main(
    tools: dict[str, LocalTool],
    name: str,
    arguments: dict[str, object],
    cancel_event,
    result_queue: Queue[dict[str, object]],
) -> None:
    _start_process_group()
    result = execute_tool(
        tools,
        name,
        arguments,
        cancel_requested=cancel_event.is_set,
    )
    result_queue.put(_pickle_safe_result(result))


def _process_context() -> BaseContext:
    if "fork" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context()


def _start_process_group() -> None:
    if os.name == "nt":
        return
    try:
        os.setsid()
    except OSError:
        pass


def _terminate_process_group(process: Process) -> None:
    if os.name != "nt" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.terminate()


def _kill_process_group(process: Process) -> None:
    if os.name != "nt" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.kill()


def _pickle_safe_result(result: dict[str, object]) -> dict[str, object]:
    try:
        pickle.dumps(result)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Tool returned a non-serializable result: {exc}",
        }
    return result
