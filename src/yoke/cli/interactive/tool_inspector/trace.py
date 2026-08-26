"""CLI compatibility exports for the shared tool trace service."""

from __future__ import annotations

from yoke.agent.models import Message
from yoke.agent.observability import ToolTraceContext as ToolTraceContext
from yoke.agent.observability import ToolTraceEntry as ToolTraceEntry
from yoke.agent.observability import ToolTraceOutputChunk as ToolTraceOutputChunk
from yoke.agent.observability import ToolTraceStore as ToolTraceStore


def entries_from_messages(messages: list[Message]) -> list[ToolTraceEntry]:
    """Build reconstructed completed traces from persisted transcript messages."""
    from yoke.agent.observability.tool_transcript import (
        entries_from_messages as reconstruct_entries,
    )

    return reconstruct_entries(messages)


def merge_trace_entries(
    completed: list[ToolTraceEntry],
    live: list[ToolTraceEntry],
) -> list[ToolTraceEntry]:
    """Merge reconstructed and live entries for the terminal inspector."""
    from yoke.agent.observability.tool_transcript import (
        merge_trace_entries as merge_entries,
    )

    return merge_entries(completed, live)
