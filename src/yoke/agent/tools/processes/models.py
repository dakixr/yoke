"""Validated requests for the shared process observation interface."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yoke.agent.tools.processes.cursor import CURSOR_LENGTH, CURSOR_PATTERN


DEFAULT_READ_WAIT_MS = 60_000
MAX_READ_WAIT_MS = 3_600_000
DEFAULT_EXEC_WAIT_MS = 30_000
MAX_EXEC_WAIT_MS = 300_000
DEFAULT_INPUT_WAIT_MS = 250
MAX_INPUT_WAIT_MS = 5_000
MAX_READ_BYTES = 64_000


class ProcessCursor(BaseModel):
    """One process identity plus an opaque retained-output continuation token."""

    model_config = ConfigDict(extra="forbid")

    session_id: int = Field(ge=1, strict=True)
    cursor: str | None = Field(
        default=None,
        min_length=CURSOR_LENGTH,
        max_length=CURSOR_LENGTH,
        pattern=CURSOR_PATTERN,
        description=(
            "Opaque continuation returned by a previous process result. Omit it to "
            "read from the earliest retained output for this session."
        ),
    )


class ProcessReadRequest(BaseModel):
    """One bounded wait and output budget shared across selected processes."""

    model_config = ConfigDict(extra="forbid")

    sessions: list[ProcessCursor] = Field(min_length=1, max_length=16)
    until: Literal["completion", "output_or_completion"] = Field(
        default="completion",
        description=(
            "completion waits for ALL selected processes to finish. "
            "output_or_completion returns when ANY has unread output or finishes."
        ),
    )
    wait_ms: int = Field(
        default=DEFAULT_READ_WAIT_MS,
        ge=0,
        le=MAX_READ_WAIT_MS,
        strict=True,
        description=(
            "Maximum wait, not a process timeout. Increase for long-running work. "
            "Zero returns a snapshot immediately; expiry never stops a process."
        ),
    )
    max_bytes: int = Field(
        default=32_000,
        ge=1_024,
        le=MAX_READ_BYTES,
        strict=True,
        description=(
            "Total UTF-8 output bytes across the batch, shared evenly. "
            "Does not shorten completion waits. Follow cursors for remaining output."
        ),
    )

    @model_validator(mode="after")
    def _unique_sessions(self) -> Self:
        if len({cursor.session_id for cursor in self.sessions}) != len(self.sessions):
            raise ValueError("Session IDs must be unique within one process_read.")
        return self
