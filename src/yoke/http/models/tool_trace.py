"""Tool inspector transport models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from yoke.http.models.common import ApiModel
from yoke.http.models.common import CursorInfo


class ToolTraceContextInfo(ApiModel):
    role: Literal["user", "assistant"]
    text: str


class ToolTraceArguments(ApiModel):
    raw: str | None = None
    executed: dict[str, object] | None = None


class ToolTraceTime(ApiModel):
    started: str | None = None
    ended: str | None = None
    duration_ms: int | None = None


class ToolTraceOutputInfo(ApiModel):
    retained_chars: int = 0
    truncated: bool = False
    latest_seq: int = 0


class ToolCallInfo(ApiModel):
    id: str
    tool_name: str
    status: Literal["pending", "running", "ok", "failed", "cancelled"]
    iteration: int | None = None
    turn_id: int | None = None
    sequence: int | None = Field(
        default=None,
        description="One-based call ordinal in canonical transcript/live merge order, before filtering.",
    )
    arguments: ToolTraceArguments
    time: ToolTraceTime
    result: dict[str, object] | None = None
    result_projection: str | None = Field(
        default=None,
        description=(
            "Redacted tool result after the same opt-in projection used for "
            "provider context. Null when this result had no provider projection."
        ),
    )
    output: ToolTraceOutputInfo
    context: list[ToolTraceContextInfo]
    after_context: list[ToolTraceContextInfo]
    retention: Literal["runtime", "session"]


class ToolCallListResponse(ApiModel):
    data: list[ToolCallInfo]
    cursor: CursorInfo
    total: int = Field(
        description="Number of calls matching the filters before paging."
    )


class ToolCallResponse(ApiModel):
    data: ToolCallInfo


class ToolOutputChunk(ApiModel):
    seq: int
    stream: str
    text: str


class ToolOutputCursor(ApiModel):
    next: int
    truncated_before: int


class ToolOutputResponse(ApiModel):
    data: list[ToolOutputChunk]
    cursor: ToolOutputCursor
