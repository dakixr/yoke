"""Process-isolated tool execution helpers."""

from __future__ import annotations

import atexit
import multiprocessing
import os
import pickle
import queue
import signal
import threading
import time
import weakref
from collections.abc import Callable
from multiprocessing.context import BaseContext
from multiprocessing.context import Process
from typing import cast
from multiprocessing.queues import Queue

from yoke.agent.loop.tool_core import cancelled_tool_result
from yoke.agent.loop.tool_core import execute_tool
from yoke.agent.loop.types import StopRequested
from yoke.agent.tools import LocalTool


TOOL_CANCEL_GRACE_SECONDS = 0.25
TOOL_CANCEL_KILL_SECONDS = 0.04
TOOL_POLL_SECONDS = 0.005
SPAWN_UNSAFE_CONTEXT_KEYS = {"cancel_requested", "provider"}
_ACTIVE_INVOCATIONS: weakref.WeakSet[ToolProcessInvocation]


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
        tool = _tool_for_child_process(tools.get(name), self._context)
        self._cancel_event = self._context.Event()
        self._result_queue: Queue[dict[str, object]] = self._context.Queue(maxsize=1)
        process_factory = cast(
            Callable[..., Process],
            getattr(self._context, "Process"),
        )
        self._process: Process = process_factory(
            target=_tool_process_main,
            args=(
                tool,
                name,
                arguments,
                self._cancel_event,
                self._result_queue,
            ),
        )
        self._result: dict[str, object] | None = None
        self._cancelled = False
        self._owner_pid = os.getpid()
        self._started = False
        self._closed = False
        self._reaping = False

    def start(self) -> None:
        """Start the child process."""
        try:
            self._process.start()
        except BaseException:
            if self._process.pid is not None:
                self._started = True
                _ACTIVE_INVOCATIONS.add(self)
                self.cancel(wait=True)
            else:
                self.close()
            raise
        self._started = True
        _ACTIVE_INVOCATIONS.add(self)

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
                    "error": (
                        f"Tool process exited with status {self._process.exitcode}"
                    ),
                }
        return True

    def result(self) -> dict[str, object]:
        """Return the final result, blocking until it is available."""
        while not self.done():
            time.sleep(TOOL_POLL_SECONDS)
        result = self._result
        if result is None:
            fallback: dict[str, object] = {
                "ok": False,
                "error": "Tool process finished without a result",
            }
            self.close()
            return fallback
        self.close()
        return result

    def cancel(self, *, wait: bool = False) -> None:
        """Request termination without blocking the interactive control path."""
        if os.getpid() != self._owner_pid:
            return
        self._cancelled = True
        self._cancel_event.set()
        if not self._started:
            self._closed = True
            self._result_queue.close()
            self._result_queue.join_thread()
            return
        if not self._process.is_alive():
            self.close()
            return
        _terminate_process_group(self._process)
        if wait:
            self._reap_cancelled_process()
        elif not self._reaping:
            self._reaping = True
            threading.Thread(
                target=self._reap_cancelled_process,
                daemon=True,
                name="yoke-tool-reaper",
            ).start()

    def _reap_cancelled_process(self) -> None:
        self._process.join(timeout=TOOL_CANCEL_KILL_SECONDS)
        if self._process.is_alive():
            _kill_process_group(self._process)
            self._process.join(timeout=TOOL_CANCEL_GRACE_SECONDS)
        self.close()

    def close(self) -> None:
        """Release multiprocessing resources."""
        if os.getpid() != self._owner_pid:
            return
        if self._closed:
            return
        if not self._started:
            self._result_queue.close()
            self._result_queue.join_thread()
            self._closed = True
            return
        if self._process.is_alive():
            self._process.join(timeout=TOOL_CANCEL_GRACE_SECONDS)
        if self._process.is_alive():
            _terminate_process_group(self._process)
            self._process.join(timeout=TOOL_CANCEL_GRACE_SECONDS)
        if self._process.is_alive():
            _kill_process_group(self._process)
            self._process.join(timeout=TOOL_CANCEL_GRACE_SECONDS)
        if self._process.is_alive():
            return
        self._process.join(timeout=0)
        self._result_queue.close()
        self._result_queue.join_thread()
        self._closed = True
        _ACTIVE_INVOCATIONS.discard(self)


_ACTIVE_INVOCATIONS = weakref.WeakSet()


def cancel_active_tool_processes() -> None:
    """Cancel any isolated tool processes still owned by this interpreter."""
    for invocation in list(_ACTIVE_INVOCATIONS):
        invocation.cancel(wait=True)


atexit.register(cancel_active_tool_processes)


def wait_for_tool_process(
    invocation: ToolProcessInvocation,
    *,
    stop_requested: StopRequested | None,
) -> tuple[dict[str, object], bool]:
    """Wait for one tool process, cancelling it if the turn stops."""
    try:
        while not invocation.done():
            if stop_requested is not None and stop_requested():
                invocation.cancel()
                return cancelled_tool_result(), True
            time.sleep(TOOL_POLL_SECONDS)
        return invocation.result(), False
    except BaseException:
        invocation.cancel(wait=True)
        raise


def _tool_process_main(
    tool: LocalTool | None,
    name: str,
    arguments: dict[str, object],
    cancel_event,
    result_queue: Queue[dict[str, object]],
) -> None:
    _start_process_group()
    tools = {tool.name: tool} if tool is not None else {}
    result = execute_tool(
        tools,
        name,
        arguments,
        cancel_requested=cancel_event.is_set,
    )
    result_queue.put(_pickle_safe_result(result))


def _process_context() -> BaseContext:
    return multiprocessing.get_context("spawn")


def _tool_for_child_process(
    tool: LocalTool | None,
    context: BaseContext,
) -> LocalTool | None:
    if tool is None:
        return None
    if context.get_start_method() == "fork":
        return tool
    child_tool = tool.model_copy(deep=False)
    child_tool._context = _spawn_safe_tool_context(tool._context)
    return child_tool


def is_tool_spawnable(tool: LocalTool) -> bool:
    """Return whether a tool can be sent to a child process."""
    context = _process_context()
    try:
        pickle.dumps(_tool_for_child_process(tool, context))
    except Exception:
        return False
    return True


def _spawn_safe_tool_context(context: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in context.items()
        if key not in SPAWN_UNSAFE_CONTEXT_KEYS and _is_spawn_safe_context_value(value)
    }


def _is_spawn_safe_context_value(value: object) -> bool:
    try:
        pickle.dumps(value)
    except RuntimeError as exc:
        return "should only be shared between processes through inheritance" in str(exc)
    except Exception:
        return False
    return True


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
            pass
        except OSError:
            pass
    process.terminate()


def _kill_process_group(process: Process) -> None:
    if os.name != "nt" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            pass
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
