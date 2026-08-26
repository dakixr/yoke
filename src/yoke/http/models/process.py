"""Runtime-scoped command process inspector transport models."""

from __future__ import annotations

from typing import Literal

from yoke.http.models.common import ApiModel


class ProcessOutputInfo(ApiModel):
    tail: str
    original_bytes: int
    retained_bytes: int
    truncated: bool
    latest_seq: int


class ProcessInfo(ApiModel):
    process_id: str
    session_id: str
    runtime_session_id: int
    pid: int
    command: str
    cwd: str
    tty: bool
    status: Literal["running", "exited", "failed"]
    started_at: str
    elapsed_ms: int
    exit_code: int | None = None
    output: ProcessOutputInfo
    retention: Literal["runtime"] = "runtime"


class ProcessListResponse(ApiModel):
    data: list[ProcessInfo]


class ProcessResponse(ApiModel):
    data: ProcessInfo


class ProcessOutputChunk(ApiModel):
    seq: int
    stream: Literal["combined"] = "combined"
    text: str


class ProcessOutputCursor(ApiModel):
    next: int
    truncated_before: int


class ProcessOutputResponse(ApiModel):
    data: list[ProcessOutputChunk]
    cursor: ProcessOutputCursor


class ProcessStdinRequest(ApiModel):
    text: str


class ProcessSignalRequest(ApiModel):
    signal: Literal["interrupt", "terminate"]
