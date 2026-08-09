"""Tools for managed command execution and background interaction."""

from __future__ import annotations

import atexit
import secrets
from pathlib import Path

from pydantic import AliasChoices
from pydantic import Field

from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)
from yoke.agent.tools.command_process_types import CommandProcessResult
from yoke.agent.tools.command_process_types import (
    DEFAULT_EXEC_YIELD_TIME_MS,
)
from yoke.agent.tools.command_process_types import (
    DEFAULT_MAX_OUTPUT_TOKENS,
)
from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.agent.truncate import truncate_tail

from .base import WorkspaceTool


_FALLBACK_COMMAND_PROCESS_MANAGER = CommandProcessManager()
atexit.register(_FALLBACK_COMMAND_PROCESS_MANAGER.close)


class ManagedCommandTool(WorkspaceTool):
    """Base class for tools that share background command sessions."""

    execute_in_process = True

    def _manager(self) -> CommandProcessManager:
        manager = self._context.get("command_process_manager")
        if isinstance(manager, CommandProcessManager):
            return manager
        return _FALLBACK_COMMAND_PROCESS_MANAGER

    def _format_result(
        self,
        result: CommandProcessResult,
        *,
        max_output_tokens: int | None,
    ) -> dict[str, object]:
        token_budget = max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
        truncation = truncate_tail(
            result.output,
            max_bytes=min(DEFAULT_MAX_BYTES, token_budget * 4),
        )
        truncation_details = truncation.to_dict()
        truncation_details.pop("content")
        running = result.session_id is not None
        ok = not result.timed_out and (running or result.exit_code == 0)
        payload: dict[str, object] = {
            "ok": ok,
            "session_id": result.session_id,
            "exit_code": result.exit_code,
            "returncode": result.exit_code,
            "running": running,
            "timed_out": result.timed_out,
            "chunk_id": secrets.token_hex(3),
            "wall_time_seconds": result.wall_time_seconds,
            "elapsed_seconds": result.wall_time_seconds,
            "original_token_count": (result.original_output_bytes + 3) // 4,
            "output": truncation.content.rstrip("\n"),
            "outputTruncationDetails": truncation_details,
        }
        if result.timed_out:
            payload["error"] = "Command timed out"
        elif not ok:
            payload["error"] = f"Command exited with status {result.exit_code}"
        return payload

    def _cancelled_result(self) -> dict[str, object]:
        truncation_details = truncate_tail("").to_dict()
        truncation_details.pop("content")
        return {
            "ok": False,
            "session_id": None,
            "exit_code": -1,
            "returncode": -1,
            "running": False,
            "cancelled": True,
            "error": "Command cancelled",
            "output": "",
            "outputTruncationDetails": truncation_details,
        }


class ExecCommandTool(ManagedCommandTool):
    """Run a command and return when it exits or yields to the background."""

    name = "exec_command"
    description = (
        "Run a command, returning output or a session ID for ongoing "
        "interaction. Use write_stdin with the returned session ID to poll "
        "or send input. Defaults to PowerShell on Windows."
    )

    cmd: str = Field(
        min_length=1,
        validation_alias=AliasChoices("cmd", "command"),
        description="Shell command to execute.",
    )
    workdir: str | None = Field(
        default=None,
        description="Working directory. Defaults to the workspace root.",
    )
    tty: bool = Field(
        default=False,
        description="Allocate a PTY for interactive terminal input.",
    )
    yield_time_ms: int = Field(
        default=DEFAULT_EXEC_YIELD_TIME_MS,
        ge=1,
        le=300_000,
        description="Wait before yielding output. Defaults to 30 seconds.",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=200_000,
        description="Approximate output token budget.",
    )
    shell: str | None = Field(
        default=None,
        description="Shell executable. Defaults to the user's shell.",
    )
    login: bool = Field(
        default=True,
        description="Use login shell semantics where supported.",
    )

    def execute(self) -> dict[str, object]:
        """Start a command and wait for completion or the yield deadline."""
        try:
            if self._is_cancel_requested():
                return self._cancelled_result()
            cwd = self.root if self.workdir is None else self._resolve_workdir()
            result = self._manager().exec_command(
                command=self.cmd,
                cwd=cwd,
                tty=self.tty,
                yield_time_ms=self.yield_time_ms,
                shell=self.shell,
                login=self.login,
                cancel_requested=self._is_cancel_requested,
            )
            return self._format_result(
                result,
                max_output_tokens=self.max_output_tokens,
            )
        except Exception as exc:
            return self._error(str(exc), command=self.cmd)

    def _resolve_workdir(self) -> Path:
        if self.workdir is None:
            return self.root
        cwd = self._resolve_path(self.workdir)
        if not cwd.is_dir():
            raise NotADirectoryError(str(cwd))
        return cwd


class WriteStdinTool(ManagedCommandTool):
    """Poll or interact with a running command session."""

    name = "write_stdin"
    description = (
        "Write characters to an existing command session, or poll it with an "
        "empty chars value, and return recent output."
    )

    session_id: int = Field(
        ge=1,
        description="Session identifier returned by exec_command.",
    )
    chars: str = Field(
        default="",
        description="Characters to write. Empty polls without writing.",
    )
    yield_time_ms: int | None = Field(
        default=None,
        ge=1,
        le=3_600_000,
        description=(
            "Wait before yielding output. Empty polls default to 5000 ms; "
            "writes default to 250 ms."
        ),
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=200_000,
        description="Approximate output token budget.",
    )

    def execute(self) -> dict[str, object]:
        """Poll a running command or send it terminal input."""
        try:
            result = self._manager().write_stdin(
                session_id=self.session_id,
                chars=self.chars,
                yield_time_ms=self.yield_time_ms,
                cancel_requested=self._is_cancel_requested,
            )
            return self._format_result(
                result,
                max_output_tokens=self.max_output_tokens,
            )
        except Exception as exc:
            return self._error(str(exc), session_id=self.session_id)


CommandTool = ExecCommandTool
