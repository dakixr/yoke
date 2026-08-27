"""Shared user-facing activity labels for agent turns."""

from __future__ import annotations


def activity_status_for_event(
    event: str,
    payload: dict[str, object],
    *,
    current: str = "Thinking",
) -> str | None:
    """Return the CLI-compatible status label produced by one runtime event."""
    if event == "provider_rate_limited":
        return "Rate limited"
    if event == "provider_retry":
        return "Retrying provider"
    if event == "provider_recovered":
        return "Thinking"
    if event in {
        "compaction_summary_start",
        "compaction_start",
        "compaction_progress",
    }:
        return "Compacting"
    if event in {
        "compaction_summary_end",
        "compaction_end",
        "context_compaction",
    }:
        return "Thinking"
    if event == "model_start":
        if current in {"Rate limited", "Retrying provider"}:
            return current
        return "Thinking"
    if event == "model_end":
        return "Streaming"
    if event == "assistant_message" and payload.get("phase") == "commentary":
        return "Streaming"
    if event == "tool_execution_start":
        return "Running tool"
    if event == "tool_execution_end":
        return "Thinking" if payload.get("ok", False) else "Recovering"
    return None
