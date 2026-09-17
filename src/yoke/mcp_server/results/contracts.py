"""Published output shapes for the composed MCP tools."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from yoke.agent.tools.processes.cursor import CURSOR_LENGTH, CURSOR_PATTERN
from yoke.mcp_server.execution.models import ResultEnvelope


class ItemOutcome(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    status: Literal["ok", "error", "skipped", "cancelled", "unknown"]
    data: dict[str, Any] | None = None
    error: str | None = None


class BatchOutput(ResultEnvelope):
    run_id: str
    items: list[ItemOutcome]
    operations: int
    elapsed_ms: int


class RetainedOutput(ResultEnvelope):
    result_ref: str
    content: str
    cursor: int
    next_cursor: int | None
    bytes: int
    complete: bool


class ExecutionOutput(ResultEnvelope):
    output: str | None = None
    session_id: int | None = None
    exit_code: int | None = None
    running: bool | None = None
    timed_out: bool | None = None
    input_written: bool | None = None
    input_bytes_written: int | None = None
    elapsed_seconds: float | None = None
    cursor: str | None = Field(
        default=None,
        min_length=CURSOR_LENGTH,
        max_length=CURSOR_LENGTH,
        pattern=CURSOR_PATTERN,
    )
    has_more_output: bool | None = None
    gap: bool | None = None
    reason: (
        Literal["completed", "output", "deadline", "snapshot", "error", "cancelled"]
        | None
    ) = None
    next_tool: str | None = None
    status: str | None = None
    error: str | None = None


class ProcessOutput(ResultEnvelope):
    reason: Literal["completed", "output", "deadline", "snapshot", "error", "cancelled"]
    items: list[ExecutionOutput]


class ProcessCancelOutput(ResultEnvelope):
    session_id: int | None = None
    status: str | None = None
    running: bool | None = None
    exit_code: int | None = None
    timed_out: bool = False


OUTPUTS = {
    "batch_read": BatchOutput,
    "result_read": RetainedOutput,
    "process_read": ProcessOutput,
    "command_exec": ExecutionOutput,
    "python_exec": ExecutionOutput,
    "process_input": ExecutionOutput,
    "process_cancel": ProcessCancelOutput,
}
