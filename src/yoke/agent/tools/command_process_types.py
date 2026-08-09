"""Shared command-process constants and value types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

MIN_YIELD_TIME_MS = 250
MAX_YIELD_TIME_MS = 30_000
DEFAULT_EXEC_YIELD_TIME_MS = 30_000
DEFAULT_POLL_YIELD_TIME_MS = 5_000
MAX_POLL_YIELD_TIME_MS = 3_600_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
MAX_PROCESS_COUNT = 64
MAX_COMPLETED_PROCESS_COUNT = 64
MAX_RETAINED_OUTPUT_BYTES = 1024 * 1024
INTERRUPT = "\x03"

CancelRequested = Callable[[], bool]


def decode_command_output_chunk(raw: bytes) -> str:
    """Decode one streamed command-output chunk."""
    if not raw:
        return ""
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass(slots=True, frozen=True)
class CommandProcessResult:
    """Output observed during one exec or stdin interaction."""

    session_id: int | None
    exit_code: int | None
    output: str
    wall_time_seconds: float
    original_output_bytes: int
    timed_out: bool = False


@dataclass(slots=True, frozen=True)
class CommandProcessSnapshot:
    """Stable, non-consuming view of one managed command process."""

    session_id: int
    pid: int
    command: str
    cwd: Path
    tty: bool
    status: Literal["running", "exited", "failed"]
    started_at: datetime
    elapsed_seconds: float
    exit_code: int | None
    output_tail: str
    original_output_bytes: int
    retained_output_bytes: int


def clamp_exec_yield_time(yield_time_ms: int) -> int:
    """Clamp an initial execution wait to supported bounds."""
    return max(MIN_YIELD_TIME_MS, min(yield_time_ms, MAX_YIELD_TIME_MS))


def clamp_write_yield_time(yield_time_ms: int | None, *, has_input: bool) -> int:
    """Resolve polling and interactive write wait bounds."""
    if yield_time_ms is None:
        return MIN_YIELD_TIME_MS if has_input else DEFAULT_POLL_YIELD_TIME_MS
    minimum = MIN_YIELD_TIME_MS if has_input else DEFAULT_POLL_YIELD_TIME_MS
    maximum = MAX_YIELD_TIME_MS if has_input else MAX_POLL_YIELD_TIME_MS
    return max(minimum, min(yield_time_ms, maximum))
