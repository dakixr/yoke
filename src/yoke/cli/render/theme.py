"""Shared visual palette and toolbar formatting helpers."""

from __future__ import annotations

import os

ACCENT = "#89bdb5"
AMBER = "#f0a030"
RED = "#d04040"
DIM = "#888888"
WHITE = "#ffffff"

TOOLBAR_STYLE_ENTRIES: dict[str, str] = {
    "bottom-toolbar": f"noinherit bg: fg:{DIM}",
    "bottom-toolbar.spinner": f"noinherit fg:{ACCENT} bold",
    "bottom-toolbar.status": f"noinherit fg:{ACCENT} bold",
    "bottom-toolbar.tokens": f"noinherit fg:{WHITE}",
    "bottom-toolbar.timer": f"noinherit fg:{DIM}",
    "bottom-toolbar.tools": f"noinherit fg:{DIM}",
    "bottom-toolbar.gauge.low": f"noinherit fg:{ACCENT}",
    "bottom-toolbar.gauge.mid": f"noinherit fg:{AMBER}",
    "bottom-toolbar.gauge.high": f"noinherit fg:{RED}",
    "bottom-toolbar.gauge.text": f"noinherit fg:{DIM}",
    "bottom-toolbar.identity": f"noinherit fg:{DIM}",
    "bottom-toolbar.title": f"noinherit fg:{DIM} italic",
    "bottom-toolbar.queue": f"noinherit fg:{AMBER}",
    "bottom-toolbar.cancel": f"noinherit fg:{RED}",
}


def format_token_count(tokens: int) -> str:
    """Format a token count as a compact string."""
    if tokens < 1_000:
        return str(tokens)
    thousands = tokens / 1_000
    if tokens % 1_000 == 0 or thousands >= 10:
        return f"{round(thousands):.0f}k"
    return f"{thousands:.1f}k"


def _segment_enabled(env_var: str, default: bool = True) -> bool:
    value = os.environ.get(env_var)
    if value is None:
        return default
    return value.lower() not in ("0", "false", "no", "off")


def show_timer() -> bool:
    """Return whether the elapsed-time toolbar segment is enabled."""
    return _segment_enabled("YOKE_BAR_TIMER")


def show_tokens() -> bool:
    """Return whether token toolbar segments are enabled."""
    return _segment_enabled("YOKE_BAR_TOKENS", default=False)


def show_gauge() -> bool:
    """Return whether context gauge toolbar segments are enabled."""
    return _segment_enabled("YOKE_BAR_GAUGE")


def show_tool_count() -> bool:
    """Return whether the per-turn tool-count segment is enabled."""
    return _segment_enabled("YOKE_BAR_TOOLS")


def show_turn_number() -> bool:
    """Return whether the active turn number segment is enabled."""
    return _segment_enabled("YOKE_BAR_TURN", default=False)
