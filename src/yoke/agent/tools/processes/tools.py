"""Native tools for input, observation, and explicit process cancellation."""

from __future__ import annotations

from pydantic import Field

from yoke.agent.tools.processes.base import ManagedCommandTool
from yoke.agent.tools.command_process_support.coordination import (
    acquired,
    ProcessOperationCancelled,
)
from yoke.agent.tools.command_process_support.input import StdinDeliveryError
from yoke.agent.tools.processes.cursor import decode_cursor
from yoke.agent.tools.processes.cursor import CURSOR_LENGTH, CURSOR_PATTERN
from yoke.agent.tools.processes.models import (
    DEFAULT_INPUT_WAIT_MS,
    MAX_INPUT_WAIT_MS,
    ProcessReadRequest,
)
from yoke.agent.tools.processes.observation import page, read_processes


class ProcessReadTool(ProcessReadRequest, ManagedCommandTool):
    """Read retained output with one bounded completion-first batch wait."""

    name = "process_read"
    description = (
        "Read retained process output. By default wait up to 60000 ms for ALL "
        "selected processes to finish, ignoring intermediate logs. Increase wait_ms "
        "for long-running jobs. Use until='output_or_completion' for interactive "
        "output from ANY selected process, or wait_ms=0 for a snapshot. "
        "Follow returned cursors while running or has_more_output, including after exit. "
        "Reads never consume shared output, send input, or stop a process."
    )

    def execute(self) -> dict[str, object]:
        result = read_processes(
            self._manager(), self, cancel_requested=self._is_cancel_requested
        )
        for item in result["items"]:
            if item.get("running") is False:
                self._mark_completion_seen(item["session_id"])
        return result


class ProcessInputTool(ManagedCommandTool):
    """Send stdin and collect the bounded immediate response without polling."""

    name = "process_input"
    provider_result_projection = "command"
    description = (
        "Send nonempty chars to process stdin, then collect output for a short "
        "response window. Use process_read for waiting or polling. Pass the last "
        "returned cursor to avoid repeating output; without it, read from the "
        "earliest retained output. Never retry input solely because a read failed."
    )

    session_id: int = Field(ge=1, strict=True)
    chars: str = Field(
        min_length=1,
        description="Nonempty stdin text. Use process_read for empty polling.",
    )
    cursor: str | None = Field(
        default=None,
        min_length=CURSOR_LENGTH,
        max_length=CURSOR_LENGTH,
        pattern=CURSOR_PATTERN,
        description=(
            "Opaque continuation returned by the previous result for this session. "
            "Omit it to read retained history from the beginning."
        ),
    )
    wait_ms: int = Field(
        default=DEFAULT_INPUT_WAIT_MS,
        ge=0,
        le=MAX_INPUT_WAIT_MS,
        strict=True,
        description="Maximum response window after writing; returns earlier on exit. Not a process timeout.",
    )
    max_output_tokens: int | None = Field(default=None, ge=1, le=200_000, strict=True)

    def execute(self) -> dict[str, object]:
        if self._is_cancel_requested():
            return self._error(
                "Process input cancelled",
                session_id=self.session_id,
                cancelled=True,
                reason="cancelled",
                input_written=False,
            )
        manager = self._manager()
        try:
            cursor = decode_cursor(self.session_id, self.cursor)
        except ValueError as exc:
            return self._error(
                str(exc),
                session_id=self.session_id,
                input_written=False,
            )
        written: bool | None = False
        try:
            managed = manager._get(self.session_id)
            # Input plus its immediate response is serialized, independently of reads.
            with acquired(managed.input_lock, self._is_cancel_requested):
                page(manager, cursor, 0)  # Validate cursor before any side effect.
                if self._is_cancel_requested():
                    return self._error(
                        "Process input cancelled",
                        session_id=self.session_id,
                        cancelled=True,
                        reason="cancelled",
                        input_written=False,
                    )
                if managed.process.poll() is not None:
                    raise ValueError("Cannot send input to a finished process")
                written = None  # An OS write error may follow a partial write.
                manager.write_input(
                    self.session_id,
                    self.chars,
                    cancel_requested=self._is_cancel_requested,
                )
                written = True
                result = self._read_one(cursor, self.wait_ms, self.max_output_tokens)
                result["input_written"] = True
                return result
        except StdinDeliveryError as exc:
            return self._error(
                str(exc),
                session_id=self.session_id,
                reason="cancelled" if exc.cancelled else "error",
                cancelled=exc.cancelled,
                input_written=None if exc.written else False,
                input_bytes_written=exc.written,
            )
        except ProcessOperationCancelled:
            return self._error(
                "Process input cancelled",
                session_id=self.session_id,
                cancelled=True,
                reason="cancelled",
                input_written=False,
            )
        except (ValueError, OSError, RuntimeError) as exc:
            return self._error(
                str(exc), session_id=self.session_id, input_written=written
            )


class ProcessCancelTool(ManagedCommandTool):
    """Explicitly terminate one process and retain its final output for inspection."""

    name = "process_cancel"
    description = (
        "Terminate one owned process tree. Finished retained sessions are safe to "
        "cancel again. Final output stays available through process_read."
    )
    session_id: int = Field(ge=1, strict=True)

    def execute(self) -> dict[str, object]:
        try:
            manager = self._manager()
            was_running = manager.snapshot(self.session_id).status == "running"
            manager.terminate(self.session_id)
            snapshot = manager.snapshot(self.session_id)
            self._mark_completion_seen(self.session_id)
            return {
                "ok": True,
                "session_id": self.session_id,
                "status": "terminated" if was_running else snapshot.status,
                "running": False,
                "exit_code": snapshot.exit_code,
                "timed_out": snapshot.timed_out,
            }
        except (ValueError, OSError, RuntimeError) as exc:
            return self._error(str(exc), session_id=self.session_id)
