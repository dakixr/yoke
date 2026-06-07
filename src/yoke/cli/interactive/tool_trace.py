"""Tool call trace models for the interactive CLI inspector."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from threading import Lock
from typing import cast

from yoke.agent.models import Message


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

    def record_event(self, event: str, payload: dict[str, object]) -> None:
        """Record one runtime event if it describes a tool call."""
        if event == "tool_execution_start":
            self.record_start(payload)
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
                cast(dict[str, object], result)
                if isinstance(result, dict)
                else None
            )
            entry.status = "ok" if payload.get("ok", False) else "failed"

    def snapshot(self) -> list[ToolTraceEntry]:
        """Return a stable snapshot of live trace entries."""
        with self._lock:
            return [
                _copy_entry(self._entries[tool_call_id])
                for tool_call_id in self._order
                if tool_call_id in self._entries
            ]


def entries_from_messages(messages: list[Message]) -> list[ToolTraceEntry]:
    """Build completed trace entries from transcript messages."""
    entries: dict[str, ToolTraceEntry] = {}
    order: list[str] = []
    for message in messages:
        if message.role == "assistant":
            for tool_call in message.tool_calls:
                if tool_call.id not in entries:
                    order.append(tool_call.id)
                entries[tool_call.id] = ToolTraceEntry(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.function.name,
                    raw_arguments=tool_call.function.arguments,
                    status="pending",
                )
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
    )


def _overlay_entry(
    base: ToolTraceEntry,
    update: ToolTraceEntry,
) -> ToolTraceEntry:
    entry = _copy_entry(base)
    entry.tool_name = update.tool_name or entry.tool_name
    entry.raw_arguments = update.raw_arguments or entry.raw_arguments
    entry.executed_arguments = (
        update.executed_arguments or entry.executed_arguments
    )
    entry.result = update.result or entry.result
    entry.iteration = update.iteration or entry.iteration
    entry.started_at = update.started_at or entry.started_at
    entry.ended_at = update.ended_at or entry.ended_at
    entry.status = update.status or entry.status
    return entry


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
