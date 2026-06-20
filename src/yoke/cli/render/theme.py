"""Shared visual palette and toolbar formatting helpers."""

from __future__ import annotations

import os
from typing import Literal

# ---------------------------------------------------------------------------
# Color palette — promotes the completion-menu cyan to the app-wide accent.
# ---------------------------------------------------------------------------

ACCENT = "#89bdb5"
AMBER = "#f0a030"
RED = "#d04040"
DIM = "#888888"
WHITE = "#ffffff"

# ---------------------------------------------------------------------------
# Prompt-toolkit toolbar style classes.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Context gauge thresholds (usage_percent — i.e. how full the context is).
# ---------------------------------------------------------------------------

GAUGE_LOW = 70  # below this usage: accent/low
GAUGE_MID = 90  # below this but >= GAUGE_LOW: amber/mid
# >= GAUGE_MID: red/high

# Soft-compaction trigger ratio from CompactionPolicy defaults.
DEFAULT_SOFT_TRIGGER_RATIO = 0.95

GaugeLevel = Literal["low", "mid", "high"]


def gauge_level(usage_percent: int) -> GaugeLevel:
    """Return the color level for a context usage percentage."""
    if usage_percent >= GAUGE_MID:
        return "high"
    if usage_percent >= GAUGE_LOW:
        return "mid"
    return "low"


def gauge_style(usage_percent: int) -> str:
    """Return the toolbar style class for a context usage percentage."""
    level = gauge_level(usage_percent)
    return f"bottom-toolbar.gauge.{level}"


# ---------------------------------------------------------------------------
# Token formatting — compact human-readable representation.
# ---------------------------------------------------------------------------


def format_token_count(tokens: int) -> str:
    """Format a token count as a compact string (e.g. 18342 → '18k')."""
    if tokens < 1_000:
        return str(tokens)
    thousands = tokens / 1_000
    if tokens % 1_000 == 0 or thousands >= 10:
        return f"{round(thousands):.0f}k"
    return f"{thousands:.1f}k"


# ---------------------------------------------------------------------------
# Phase strings — parallel, short, gerund form.
# ---------------------------------------------------------------------------

PHASE_THINKING = "Thinking"
PHASE_STREAMING = "Streaming"
PHASE_RUNNING_TOOL = "Running tool"
PHASE_COMPACTING = "Compacting"
PHASE_RECOVERING = "Recovering"
PHASE_IDLE = ""

# ---------------------------------------------------------------------------
# Configurable toolbar segments via environment variables.
# ---------------------------------------------------------------------------


def _segment_enabled(env_var: str, default: bool = True) -> bool:
    """Check whether a toolbar segment is enabled (env var: YOKE_BAR_*)."""
    value = os.environ.get(env_var)
    if value is None:
        return default
    return value.lower() not in ("0", "false", "no", "off")


def show_timer() -> bool:
    return _segment_enabled("YOKE_BAR_TIMER")


def show_tokens() -> bool:
    return _segment_enabled("YOKE_BAR_TOKENS", default=False)


def show_gauge() -> bool:
    return _segment_enabled("YOKE_BAR_GAUGE")


def show_tool_count() -> bool:
    return _segment_enabled("YOKE_BAR_TOOLS")


def show_turn_number() -> bool:
    return _segment_enabled("YOKE_BAR_TURN", default=False)
