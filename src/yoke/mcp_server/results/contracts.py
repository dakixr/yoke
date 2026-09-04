"""Published output shapes for the composed MCP tools."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

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


class ProcessOutput(ResultEnvelope):
    items: list[dict[str, Any]]


class ExecutionOutput(ResultEnvelope):
    output: str | None = None
    session_id: int | None = None
    exit_code: int | None = None
    running: bool | None = None
    status: str | None = None
    error: str | None = None
    result_ref: str | None = None
    preview: str | None = None


OUTPUTS = {
    "batch_read": BatchOutput,
    "result_read": RetainedOutput,
    "process_read": ProcessOutput,
    "exec_python": ExecutionOutput,
}
