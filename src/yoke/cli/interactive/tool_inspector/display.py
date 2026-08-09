"""Display labels for the interactive tool inspector."""

from __future__ import annotations


def status_icon(status: str) -> str:
    """Return a compact status icon."""
    if status == "ok":
        return "✓"
    if status == "failed":
        return "✗"
    if status == "cancelled":
        return "■"
    if status == "running":
        return "…"
    return "?"


def sidebar_style(status: str, active_pane: str) -> str:
    """Return a prompt-toolkit style for a sidebar row."""
    if active_pane != "sidebar":
        return "ansibrightblack"
    if status == "ok":
        return "ansigreen"
    if status == "failed":
        return "ansired"
    if status == "cancelled":
        return "ansibrightblack"
    return "ansiyellow"


def status_label(status: str) -> str:
    """Return the readable status label."""
    return {
        "ok": "success",
        "failed": "failed",
        "cancelled": "cancelled",
        "running": "running",
        "pending": "pending",
    }.get(status, status)


def format_duration(duration: float | None) -> str:
    """Format a duration for compact display."""
    if duration is None:
        return ""
    if duration < 10:
        return f"{duration:.1f}s"
    return f"{duration:.0f}s"
