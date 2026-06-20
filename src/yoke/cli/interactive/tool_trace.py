"""Tool call trace models for the interactive CLI inspector."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal
from typing import cast

from yoke.agent.models import Message


MAX_LIVE_OUTPUT_CHARS = 50_000


@dataclass(slots=True)
class ToolTraceContext:
    """Conversation context shown near a tool call."""

    role: Literal["user", "assistant"]
    text: str


@dataclass(slots=True)
class ToolTraceOutputChunk:
    """A streamed output chunk produced while a tool is running."""

    stream: str
    text: str


@dataclass(slots=True)
class ToolTraceEntry:
    """A user-visible tool call trace entry."""

    tool_call_id: str
    tool_name: str
    raw_arguments: str | None = None
    executed_arguments: dict[str, object] | None = None
    result: dict[str, object] | None = None
    iteration: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    status: str = "pending"
    context: list[ToolTraceContext] | None = None
    after_context: list[ToolTraceContext] | None = None
    output_chunks: list[ToolTraceOutputChunk] | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Return the observed tool duration when available."""
        if self.started_at is None:
            return None
        end = self.ended_at or time.monotonic()
        return max(0.0, end - self.started_at)


class ToolTraceStore:
    """Thread-safe live trace store for prompt-toolkit tool events."""

    def __init__(self) -> None:
        self._entries: dict[str, ToolTraceEntry] = {}
        self._order: list[str] = []
        self._lock = Lock()
        self._subscribers: list[Callable[[], None]] = []
        self._version = 0

    def record_event(self, event: str, payload: dict[str, object]) -> None:
        """Record one runtime event if it describes a tool call."""
        if event == "tool_execution_start":
            self.record_start(payload)
            return
        if event == "tool_execution_output_delta":
            self.record_output_delta(payload)
            return
        if event == "tool_execution_end":
            self.record_end(payload)

    def record_start(self, payload: dict[str, object]) -> None:
        """Record a tool start event."""
        tool_call_id = _payload_text(payload, "tool_call_id")
        tool_name = _payload_text(payload, "tool_name") or "tool"
        if not tool_call_id:
            return
        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is None:
                entry = ToolTraceEntry(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )
                self._entries[tool_call_id] = entry
                self._order.append(tool_call_id)
            entry.tool_name = tool_name
            entry.raw_arguments = _payload_text(payload, "tool_arguments")
            entry.iteration = _payload_int(payload, "iteration")
            entry.started_at = time.monotonic()
            entry.status = "running"
            subscribers = self._changed_locked()
        self._notify(subscribers)

    def record_output_delta(self, payload: dict[str, object]) -> None:
        """Record a streamed output chunk for a running tool."""
        tool_call_id = _payload_text(payload, "tool_call_id")
        tool_name = _payload_text(payload, "tool_name") or "tool"
        text = _payload_text(payload, "text")
        if not tool_call_id or not text:
            return
        stream = _payload_text(payload, "stream") or "output"
        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is None:
                entry = ToolTraceEntry(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )
                self._entries[tool_call_id] = entry
                self._order.append(tool_call_id)
            entry.tool_name = tool_name
            entry.iteration = _payload_int(payload, "iteration") or entry.iteration
            if entry.started_at is None:
                entry.started_at = time.monotonic()
            if entry.status == "pending":
                entry.status = "running"
            chunks = entry.output_chunks if entry.output_chunks is not None else []
            chunks.append(ToolTraceOutputChunk(stream=stream, text=text))
            _trim_output_chunks(chunks)
            entry.output_chunks = chunks
            subscribers = self._changed_locked()
        self._notify(subscribers)

    def record_end(self, payload: dict[str, object]) -> None:
        """Record a tool completion event."""
        tool_call_id = _payload_text(payload, "tool_call_id")
        tool_name = _payload_text(payload, "tool_name") or "tool"
        if not tool_call_id:
            return
        result = payload.get("result")
        executed_arguments = payload.get("executed_arguments")
        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is None:
                entry = ToolTraceEntry(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )
                self._entries[tool_call_id] = entry
                self._order.append(tool_call_id)
            entry.tool_name = tool_name
            entry.iteration = _payload_int(payload, "iteration")
            entry.ended_at = time.monotonic()
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

    def snapshot(self) -> list[ToolTraceEntry]:
        """Return a stable snapshot of live trace entries."""
        with self._lock:
            return [
                _copy_entry(self._entries[tool_call_id])
                for tool_call_id in self._order
                if tool_call_id in self._entries
            ]

    def version(self) -> int:
        """Return a monotonically increasing trace version."""
        with self._lock:
            return self._version

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call callback after future trace changes and return an unsubscribe hook."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _changed_locked(self) -> list[Callable[[], None]]:
        self._version += 1
        return list(self._subscribers)

    def _notify(self, subscribers: list[Callable[[], None]]) -> None:
        for callback in subscribers:
            callback()


def entries_from_messages(messages: list[Message]) -> list[ToolTraceEntry]:
    """Build completed trace entries from transcript messages."""
    entries: dict[str, ToolTraceEntry] = {}
    order: list[str] = []
    recent_user_text: str | None = None
    pending_user_context = False
    last_tool_call_id: str | None = None
    for message in messages:
        if message.role == "user":
            recent_user_text = message.text_content()
            pending_user_context = bool(recent_user_text)
            continue
        if message.role == "assistant":
            assistant_text = message.text_content()
            if not message.tool_calls:
                if (
                    message.phase != "commentary"
                    and assistant_text
                    and last_tool_call_id in entries
                ):
                    entry = entries[last_tool_call_id]
                    entry.after_context = [
                        *(entry.after_context or []),
                        ToolTraceContext(role="assistant", text=assistant_text),
                    ]
                continue
            for index, tool_call in enumerate(message.tool_calls):
                if tool_call.id not in entries:
                    order.append(tool_call.id)
                entries[tool_call.id] = ToolTraceEntry(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.function.name,
                    raw_arguments=tool_call.function.arguments,
                    status="pending",
                    context=_message_context(
                        user_text=recent_user_text if pending_user_context else None,
                    )
                    if index == 0
                    else None,
                )
                last_tool_call_id = tool_call.id
            if message.tool_calls:
                pending_user_context = False
            continue
        if message.role != "tool" or message.tool_call_id is None:
            continue
        entry = entries.get(message.tool_call_id)
        if entry is None:
            entry = ToolTraceEntry(
                tool_call_id=message.tool_call_id,
                tool_name="tool",
            )
            entries[message.tool_call_id] = entry
            order.append(message.tool_call_id)
        result = _parse_result(message.plain_text_content)
        entry.result = result
        entry.status = "ok" if result.get("ok", True) else "failed"
        last_tool_call_id = message.tool_call_id
    return [entries[tool_call_id] for tool_call_id in order]


def merge_trace_entries(
    completed: list[ToolTraceEntry],
    live: list[ToolTraceEntry],
) -> list[ToolTraceEntry]:
    """Merge persisted and live trace entries without duplicating ids."""
    merged: dict[str, ToolTraceEntry] = {}
    order: list[str] = []
    for entry in [*completed, *live]:
        if entry.tool_call_id not in merged:
            order.append(entry.tool_call_id)
            merged[entry.tool_call_id] = _copy_entry(entry)
            continue
        merged[entry.tool_call_id] = _overlay_entry(
            merged[entry.tool_call_id],
            entry,
        )
    return [merged[tool_call_id] for tool_call_id in order]


def _copy_entry(entry: ToolTraceEntry) -> ToolTraceEntry:
    return ToolTraceEntry(
        tool_call_id=entry.tool_call_id,
        tool_name=entry.tool_name,
        raw_arguments=entry.raw_arguments,
        executed_arguments=dict(entry.executed_arguments)
        if entry.executed_arguments is not None
        else None,
        result=dict(entry.result) if entry.result is not None else None,
        iteration=entry.iteration,
        started_at=entry.started_at,
        ended_at=entry.ended_at,
        status=entry.status,
        context=list(entry.context) if entry.context is not None else None,
        after_context=list(entry.after_context)
        if entry.after_context is not None
        else None,
        output_chunks=list(entry.output_chunks)
        if entry.output_chunks is not None
        else None,
    )


def _overlay_entry(
    base: ToolTraceEntry,
    update: ToolTraceEntry,
) -> ToolTraceEntry:
    entry = _copy_entry(base)
    entry.tool_name = update.tool_name or entry.tool_name
    entry.raw_arguments = update.raw_arguments or entry.raw_arguments
    entry.executed_arguments = update.executed_arguments or entry.executed_arguments
    entry.result = update.result or entry.result
    entry.iteration = update.iteration or entry.iteration
    entry.started_at = update.started_at or entry.started_at
    entry.ended_at = update.ended_at or entry.ended_at
    entry.status = update.status or entry.status
    entry.context = update.context or entry.context
    entry.after_context = update.after_context or entry.after_context
    entry.output_chunks = update.output_chunks or entry.output_chunks
    return entry


def _message_context(
    *,
    user_text: str | None,
) -> list[ToolTraceContext] | None:
    context = []
    if user_text:
        context.append(ToolTraceContext(role="user", text=user_text))
    return context or None


def _parse_result(content: str | None) -> dict[str, object]:
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"ok": True, "content": content}
    return parsed if isinstance(parsed, dict) else {"ok": True, "value": parsed}


def _payload_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _payload_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) else None


def _trim_output_chunks(chunks: list[ToolTraceOutputChunk]) -> None:
    total = sum(len(chunk.text) for chunk in chunks)
    while chunks and total > MAX_LIVE_OUTPUT_CHARS:
        excess = total - MAX_LIVE_OUTPUT_CHARS
        first = chunks[0]
        if len(first.text) <= excess:
            total -= len(first.text)
            chunks.pop(0)
            continue
        first.text = first.text[excess:]
        return
