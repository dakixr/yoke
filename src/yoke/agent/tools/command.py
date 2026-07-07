"""Tools for managed command execution and background interaction."""

from __future__ import annotations

import queue
import secrets
import threading

from pydantic import AliasChoices
from pydantic import Field

from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.command_process import CommandProcessResult
from yoke.agent.tools.command_process import DEFAULT_EXEC_YIELD_TIME_MS
from yoke.agent.tools.command_process import DEFAULT_MAX_OUTPUT_TOKENS
from yoke.agent.tools.command_process import decode_command_output_chunk
from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.agent.truncate import truncate_tail

from .base import WorkspaceTool


class _ManagedCommandTool(WorkspaceTool):
    execute_in_process = True

    def _manager(self) -> CommandProcessManager:
        manager = self._context.get("command_process_manager")
        if isinstance(manager, CommandProcessManager):
            return manager
        manager = CommandProcessManager()
        self._context["command_process_manager"] = manager
        return manager

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
        ok = running or result.exit_code == 0
        payload: dict[str, object] = {
            "ok": ok,
            "session_id": result.session_id,
            "exit_code": result.exit_code,
            "running": running,
            "chunk_id": secrets.token_hex(3),
            "wall_time_seconds": result.wall_time_seconds,
            "original_token_count": (result.original_output_bytes + 3) // 4,
            "output": truncation.content.rstrip("\n"),
            "outputTruncationDetails": truncation_details,
        }
        if not ok:
            payload["error"] = f"Command exited with status {result.exit_code}"
        return payload


class ExecCommandTool(_ManagedCommandTool):
    """Run a command and return when it exits or yields to the background."""

    name = "exec_command"
    description = (
        "Run a command, returning output or a session ID for ongoing interaction. "
        "Use write_stdin with the returned session ID to poll or send input."
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
        le=7_200_000,
        description="Wait before yielding output. Effective range is 250-7200000 ms.",
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
        description="Use login shell semantics.",
    )

    def execute(self) -> dict[str, object]:
        """Start a command and wait for completion or the yield deadline."""
        try:
            cwd = (
                self.root if self.workdir is None else self._resolve_path(self.workdir)
            )
            if not cwd.is_dir():
                raise NotADirectoryError(str(cwd))
            result = self._manager().exec_command(
                command=self.cmd,
                cwd=cwd,
                tty=self.tty,
                yield_time_ms=self.yield_time_ms,
                shell=self.shell,
                login=self.login,
                tool_event=lambda event, payload: self._emit_tool_event(event, payload),
                cancel_requested=self._is_cancel_requested,
            )
            return self._format_result(
                result,
                max_output_tokens=self.max_output_tokens,
            )
        except Exception as exc:
            return self._error(str(exc), command=self.cmd)


class WriteStdinTool(_ManagedCommandTool):
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
        le=7_200_000,
        description=(
            "Wait before yielding output. Empty polls and writes default to "
            "30000 ms. Effective maximum is 7200000 ms."
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


# Python callers importing the former class keep working while the model-facing
# tool is universally named exec_command.
CommandTool = ExecCommandTool


class _ProcessOutputReader:
    """Compatibility reader used by the dedicated Python execution tool."""

    def __init__(self, process) -> None:
        self._process = process
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._stdout_parts: list[str] = []
        self._stderr_parts: list[str] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for stream, pipe in (
            ("stdout", self._process.stdout),
            ("stderr", self._process.stderr),
        ):
            if pipe is None:
                continue
            self._threads.append(
                threading.Thread(
                    target=self._read_stream,
                    args=(stream, pipe),
                    daemon=True,
                )
            )
        for thread in self._threads:
            thread.start()

    def emit_pending(self, emit_chunk) -> None:
        while True:
            try:
                stream, raw = self._queue.get_nowait()
            except queue.Empty:
                return
            text = decode_command_output_chunk(raw)
            if stream == "stderr":
                self._stderr_parts.append(text)
            else:
                self._stdout_parts.append(text)
            emit_chunk(stream, text)

    def finish(self, emit_chunk) -> tuple[str, str]:
        for thread in self._threads:
            thread.join(timeout=1)
        self.emit_pending(emit_chunk)
        return (
            _normalize_output("".join(self._stdout_parts)),
            _normalize_output("".join(self._stderr_parts)),
        )

    def _read_stream(self, stream: str, pipe) -> None:
        while raw := pipe.readline():
            self._queue.put((stream, raw))
        remainder = pipe.read()
        if remainder:
            self._queue.put((stream, remainder))


def _normalize_output(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
