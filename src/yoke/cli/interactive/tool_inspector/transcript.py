"""Transcript reconstruction and merging for tool inspector traces."""

from __future__ import annotations

import json

from yoke.agent.models import Message
from yoke.cli.interactive.tool_inspector.trace import _copy_entry
from yoke.cli.interactive.tool_inspector.trace import ToolTraceContext
from yoke.cli.interactive.tool_inspector.trace import ToolTraceEntry


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
                        user_text=(recent_user_text if pending_user_context else None),
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
    entry.turn_id = update.turn_id or entry.turn_id
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
