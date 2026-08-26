"""Thread-safe tool-call trace state shared by CLI and HTTP runtimes."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from threading import Lock
from typing import Literal
from typing import cast


MAX_LIVE_OUTPUT_CHARS = 50_000
LIVE_OUTPUT_TRIM_TARGET = 45_000
MAX_COMPACT_CHUNK_CHARS = 4_096


@dataclass(slots=True)
class ToolTraceContext:
    role: Literal["user", "assistant"]
    text: str


@dataclass(slots=True)
class ToolTraceOutputChunk:
    stream: str
    text: str
    seq: int = 0


@dataclass(slots=True, frozen=True)
class ToolTraceOutputPage:
    chunks: tuple[ToolTraceOutputChunk, ...]
    latest_seq: int
    truncated_before_seq: int


@dataclass(slots=True)
class ToolTraceEntry:
    tool_call_id: str
    tool_name: str
    raw_arguments: str | None = None
    executed_arguments: dict[str, object] | None = None
    result: dict[str, object] | None = None
    iteration: int | None = None
    turn_id: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    started_wall_at: str | None = None
    ended_wall_at: str | None = None
    status: str = "pending"
    context: list[ToolTraceContext] | None = None
    after_context: list[ToolTraceContext] | None = None
    output_chunks: list[ToolTraceOutputChunk] | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at or time.monotonic()
        return max(0.0, end - self.started_at)


class ToolTraceStore:
    """Retain bounded live tool traces and reject callbacks from retired turns."""

    def __init__(self) -> None:
        self._entries: dict[str, ToolTraceEntry] = {}
        self._order: list[str] = []
        self._retired_turn_ids: set[int] = set()
        self._lock = Lock()
        self._subscribers: list[Callable[[], None]] = []
        self._output_char_counts: dict[str, int] = {}
        self._output_events: dict[str, deque[ToolTraceOutputChunk]] = {}
        self._output_event_chars: dict[str, int] = {}
        self._next_output_seq: dict[str, int] = {}
        self._truncated_before_seq: dict[str, int] = {}
        self._version = 0

    def record_event(self, event: str, payload: dict[str, object]) -> None:
        if event == "tool_execution_start":
            self.record_start(payload)
            return
        if event == "tool_execution_output_delta":
            self.record_output_delta(payload)
            return
        if event == "tool_execution_end":
            self.record_end(payload)

    def record_start(self, payload: dict[str, object]) -> None:
        tool_call_id = _payload_text(payload, "tool_call_id")
        tool_name = _payload_text(payload, "tool_name") or "tool"
        if not tool_call_id:
            return
        with self._lock:
            turn_id = _payload_int(payload, "turn_id")
            if turn_id in self._retired_turn_ids:
                return
            entry = self._entry_locked(tool_call_id, tool_name)
            entry.tool_name = tool_name
            entry.raw_arguments = _payload_text(payload, "tool_arguments")
            entry.iteration = _payload_int(payload, "iteration")
            entry.turn_id = turn_id
            entry.started_at = time.monotonic()
            entry.started_wall_at = datetime.now(UTC).isoformat()
            entry.status = "running"
            subscribers = self._changed_locked()
        self._notify(subscribers)

    def record_output_delta(self, payload: dict[str, object]) -> None:
        tool_call_id = _payload_text(payload, "tool_call_id")
        tool_name = _payload_text(payload, "tool_name") or "tool"
        text = _payload_text(payload, "text")
        if not tool_call_id or not text:
            return
        stream = _payload_text(payload, "stream") or "output"
        with self._lock:
            turn_id = _payload_int(payload, "turn_id")
            if turn_id in self._retired_turn_ids:
                return
            entry = self._entry_locked(tool_call_id, tool_name)
            entry.tool_name = tool_name
            entry.iteration = _payload_int(payload, "iteration") or entry.iteration
            entry.turn_id = turn_id or entry.turn_id
            if entry.started_at is None:
                entry.started_at = time.monotonic()
                entry.started_wall_at = datetime.now(UTC).isoformat()
            if entry.status == "pending":
                entry.status = "running"
            seq = self._next_output_seq.get(tool_call_id, 1)
            self._next_output_seq[tool_call_id] = seq + 1
            self._append_cursor_output_locked(tool_call_id, stream, text, seq)
            chunks = entry.output_chunks if entry.output_chunks is not None else []
            current_size = self._output_char_counts.get(
                tool_call_id,
                sum(len(chunk.text) for chunk in chunks),
            )
            self._output_char_counts[tool_call_id] = _append_compact_output_chunk(
                chunks,
                stream=stream,
                text=text,
                current_size=current_size,
                seq=seq,
            )
            entry.output_chunks = chunks
            subscribers = self._changed_locked()
        self._notify(subscribers)

    def record_end(self, payload: dict[str, object]) -> None:
        tool_call_id = _payload_text(payload, "tool_call_id")
        tool_name = _payload_text(payload, "tool_name") or "tool"
        if not tool_call_id:
            return
        result = payload.get("result")
        executed_arguments = payload.get("executed_arguments")
        with self._lock:
            turn_id = _payload_int(payload, "turn_id")
            if turn_id in self._retired_turn_ids:
                return
            entry = self._entry_locked(tool_call_id, tool_name)
            entry.tool_name = tool_name
            entry.iteration = _payload_int(payload, "iteration")
            entry.turn_id = turn_id or entry.turn_id
            entry.ended_at = time.monotonic()
            entry.ended_wall_at = datetime.now(UTC).isoformat()
            entry.executed_arguments = (
                cast(dict[str, object], executed_arguments)
                if isinstance(executed_arguments, dict)
                else entry.executed_arguments
            )
            entry.result = (
                cast(dict[str, object], result) if isinstance(result, dict) else None
            )
            entry.status = "ok" if payload.get("ok", False) else "failed"
            subscribers = self._changed_locked()
        self._notify(subscribers)

    def retire_turn(self, turn_id: int) -> None:
        ended_at = time.monotonic()
        ended_wall_at = datetime.now(UTC).isoformat()
        changed = False
        with self._lock:
            self._retired_turn_ids.add(turn_id)
            for entry in self._entries.values():
                if entry.turn_id != turn_id or entry.status not in {"pending", "running"}:
                    continue
                entry.ended_at = ended_at
                entry.ended_wall_at = ended_wall_at
                entry.result = {
                    "ok": False,
                    "cancelled": True,
                    "error": "Tool execution cancelled with its turn.",
                }
                entry.status = "cancelled"
                changed = True
            subscribers = self._changed_locked() if changed else []
        self._notify(subscribers)

    def snapshot(self) -> list[ToolTraceEntry]:
        with self._lock:
            return [
                _copy_entry(self._entries[tool_call_id])
                for tool_call_id in self._order
                if tool_call_id in self._entries
            ]

    def get(self, tool_call_id: str) -> ToolTraceEntry | None:
        with self._lock:
            entry = self._entries.get(tool_call_id)
            return _copy_entry(entry) if entry is not None else None

    def output_page(
        self,
        tool_call_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> ToolTraceOutputPage:
        with self._lock:
            if tool_call_id not in self._entries:
                raise KeyError(tool_call_id)
            events = self._output_events.get(tool_call_id, deque())
            chunks = tuple(
                ToolTraceOutputChunk(chunk.stream, chunk.text, chunk.seq)
                for chunk in events
                if chunk.seq > after_seq
            )[:limit]
            return ToolTraceOutputPage(
                chunks=chunks,
                latest_seq=self._next_output_seq.get(tool_call_id, 1) - 1,
                truncated_before_seq=self._truncated_before_seq.get(tool_call_id, 0),
            )

    def version(self) -> int:
        with self._lock:
            return self._version

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _entry_locked(self, tool_call_id: str, tool_name: str) -> ToolTraceEntry:
        entry = self._entries.get(tool_call_id)
        if entry is None:
            entry = ToolTraceEntry(tool_call_id=tool_call_id, tool_name=tool_name)
            self._entries[tool_call_id] = entry
            self._order.append(tool_call_id)
        return entry

    def _append_cursor_output_locked(
        self,
        tool_call_id: str,
        stream: str,
        text: str,
        seq: int,
    ) -> None:
        events = self._output_events.setdefault(tool_call_id, deque())
        events.append(ToolTraceOutputChunk(stream=stream, text=text, seq=seq))
        size = self._output_event_chars.get(tool_call_id, 0) + len(text)
        while events and size > MAX_LIVE_OUTPUT_CHARS:
            dropped = events.popleft()
            size -= len(dropped.text)
            self._truncated_before_seq[tool_call_id] = dropped.seq
        self._output_event_chars[tool_call_id] = size

    def _changed_locked(self) -> list[Callable[[], None]]:
        self._version += 1
        return list(self._subscribers)

    @staticmethod
    def _notify(subscribers: list[Callable[[], None]]) -> None:
        for callback in subscribers:
            with suppress(Exception):
                callback()


def _copy_entry(entry: ToolTraceEntry) -> ToolTraceEntry:
    return ToolTraceEntry(
        tool_call_id=entry.tool_call_id,
        tool_name=entry.tool_name,
        raw_arguments=entry.raw_arguments,
        executed_arguments=(
            dict(entry.executed_arguments)
            if entry.executed_arguments is not None
            else None
        ),
        result=dict(entry.result) if entry.result is not None else None,
        iteration=entry.iteration,
        turn_id=entry.turn_id,
        started_at=entry.started_at,
        ended_at=entry.ended_at,
        started_wall_at=entry.started_wall_at,
        ended_wall_at=entry.ended_wall_at,
        status=entry.status,
        context=list(entry.context) if entry.context is not None else None,
        after_context=(
            list(entry.after_context) if entry.after_context is not None else None
        ),
        output_chunks=(
            [ToolTraceOutputChunk(chunk.stream, chunk.text, chunk.seq) for chunk in entry.output_chunks]
            if entry.output_chunks is not None
            else None
        ),
    )


def _payload_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _payload_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) else None


def _append_compact_output_chunk(
    chunks: list[ToolTraceOutputChunk],
    *,
    stream: str,
    text: str,
    current_size: int,
    seq: int,
) -> int:
    if (
        chunks
        and chunks[-1].stream == stream
        and len(chunks[-1].text) + len(text) <= MAX_COMPACT_CHUNK_CHARS
    ):
        chunks[-1].text += text
        chunks[-1].seq = seq
    else:
        chunks.append(ToolTraceOutputChunk(stream=stream, text=text, seq=seq))
    total = current_size + len(text)
    removed = 0
    trimming = total > MAX_LIVE_OUTPUT_CHARS
    while chunks and trimming and total > LIVE_OUTPUT_TRIM_TARGET:
        excess = total - LIVE_OUTPUT_TRIM_TARGET
        first = chunks[removed]
        if len(first.text) <= excess:
            total -= len(first.text)
            removed += 1
            continue
        first.text = first.text[excess:]
        total -= excess
        break
    if removed:
        del chunks[:removed]
    return total
