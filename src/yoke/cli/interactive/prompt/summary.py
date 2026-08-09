"""Prompt-toolkit turn summary helpers."""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Format a duration for the 'Worked for ...' summary line."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{remaining:02d}s"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h{remaining_minutes:02d}m"


def format_turn_summary(summary: dict[str, object]) -> str | None:
    """Format a dim turn summary line from renderer summary data."""
    duration = summary.get("duration_seconds")
    if not isinstance(duration, int | float):
        return None
    text = f"Worked for {format_duration(duration)}"
    tools = summary.get("tool_count")
    if isinstance(tools, int) and tools > 0:
        text += f" \u00b7 {tools} tool{'s' if tools != 1 else ''}"
    return text
