"""Shared execution policy and runtime binding for process tools."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from pydantic import ConfigDict, Field

from yoke.agent.tools.base import LocalTool, WorkspaceTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_support.coordination import (
    ProcessOperationCancelled,
)
from yoke.agent.tools.command_process_types import DEFAULT_MAX_OUTPUT_TOKENS
from yoke.agent.tools.command_process_types import command_completion_event_id
from yoke.agent.tools.processes.cursor import encode_cursor
from yoke.agent.tools.processes.cursor import ProcessPosition
from yoke.agent.tools.processes.models import (
    DEFAULT_EXEC_WAIT_MS,
    MAX_READ_BYTES,
    ProcessCursor,
    ProcessReadRequest,
)
from yoke.agent.tools.processes.observation import read_processes


_FALLBACK_MANAGER = CommandProcessManager()
atexit.register(_FALLBACK_MANAGER.close)


class ManagedCommandTool(LocalTool):
    """Bind every process operation to the same runtime and retained history."""

    model_config = ConfigDict(extra="forbid")
    execute_in_process = True
    preserve_cancelled_result: ClassVar[bool] = True

    def _error(self, error: str, **payload: object) -> dict[str, object]:
        if len(error) > 2048:
            error = error[:2048] + " [error text truncated]"
        return {"ok": False, "reason": "error", "error": error, **payload}

    def _manager(self) -> CommandProcessManager:
        manager = self._context.get("command_process_manager")
        return (
            manager if isinstance(manager, CommandProcessManager) else _FALLBACK_MANAGER
        )

    def _mark_completion_seen(self, session_id: int) -> None:
        seen = self._context.get("seen_command_completion_events")
        if isinstance(seen, set):
            cast(set[str], seen).update(
                command_completion_event_id(event)
                for event in self._manager().completion_events()
                if event.session_id == session_id
            )

    def _read_one(
        self, cursor: ProcessPosition, wait_ms: int, max_output_tokens: int | None
    ) -> dict[str, Any]:
        # Execution/input allow smaller singleton pages than public batch reads.
        request = ProcessReadRequest.model_construct(
            sessions=[
                ProcessCursor(
                    session_id=cursor.session_id,
                    cursor=encode_cursor(cursor),
                )
            ],
            until="completion",
            wait_ms=wait_ms,
            max_bytes=min(
                MAX_READ_BYTES, (max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS) * 4
            ),
        )
        read = read_processes(
            self._manager(), request, cancel_requested=self._is_cancel_requested
        )
        result: dict[str, Any] = {**read["items"][0], "reason": read["reason"]}
        if read.get("cancelled"):
            result.update(ok=False, cancelled=True, error=read["error"])
        if result.get("running") is False:
            self._mark_completion_seen(cursor.session_id)
        return result

    def _cancelled_result(self) -> dict[str, object]:
        return {
            "ok": False,
            "session_id": None,
            "running": False,
            "cancelled": True,
            "error": "Command cancelled",
            "output": "",
            "reason": "cancelled",
        }


class ManagedExecutionTool(ManagedCommandTool, WorkspaceTool):
    """Start work once, then observe it with the shared completion policy."""

    provider_result_projection = "command"

    mode: Literal["auto", "background"] = Field(
        default="auto",
        description=(
            "auto uses the host's normal initial completion wait. background returns "
            "a process handle immediately for interactive or deliberately concurrent "
            "work. Use process_read(wait_ms=...) to choose longer waits after a handle exists."
        ),
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=200_000,
        strict=True,
        description="Approximate output budget. Remaining output stays pageable through cursor.",
    )

    def _execution_wait_ms(self) -> int:
        if self.mode == "background":
            return 0
        configured = self._context.get("default_exec_wait_ms")
        if isinstance(configured, int) and not isinstance(configured, bool):
            return max(0, configured)
        return DEFAULT_EXEC_WAIT_MS

    def _start(
        self,
        *,
        command: str,
        cwd: Path,
        tty: bool = False,
        shell: str | None = None,
        login: bool = True,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        manager = self._manager()
        try:
            managed = manager._spawn(
                command,
                cwd,
                tty,
                shell,
                login,
                argv=argv,
                env=env,
                timeout_seconds=timeout_seconds,
                cancel_requested=self._is_cancel_requested,
            )
        except ProcessOperationCancelled:
            return self._cancelled_result()
        manager._mark_background(managed.session_id)
        result = self._read_one(
            ProcessPosition(session_id=managed.session_id),
            self._execution_wait_ms(),
            self.max_output_tokens,
        )
        if result.get("ok"):
            if result.get("timed_out"):
                result.update(ok=False, error="Command timed out")
            elif not result.get("running") and result.get("exit_code") != 0:
                result.update(
                    ok=False, error=f"Command exited with status {result['exit_code']}"
                )
        return result
