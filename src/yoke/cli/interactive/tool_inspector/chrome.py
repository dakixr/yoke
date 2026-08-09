"""Title, pane header, and footer rendering for the tool inspector."""

from __future__ import annotations

from typing import Literal
from typing import Protocol

from yoke.cli.interactive.tool_inspector.styles import fit
from yoke.cli.interactive.tool_inspector.styles import pane_label
from yoke.cli.interactive.tool_inspector.trace import ToolTraceContext
from yoke.cli.interactive.tool_inspector.trace import ToolTraceEntry


type ToolInspectorItem = ToolTraceEntry | ToolTraceContext


class ChromeState(Protocol):
    """Inspector state needed to render surrounding chrome."""

    detail_scroll: int
    search: str
    searching: bool
    notice: str
    active_pane: Literal["sidebar", "detail"]


def title(columns: int) -> str:
    """Render the inspector shortcut title."""
    return fit(
        "Tool Inspector - Ctrl+O  "
        "←/→ panes  ↑/↓ move  g/G top/bottom  PgUp/PgDn page  "
        "/ search  r raw  w wrap  y copy  q close",
        columns,
    )


def pane_header(
    state: ChromeState,
    list_width: int,
    detail_width: int,
    *,
    html: bool,
) -> str:
    """Render focus-aware pane labels."""
    tools = pane_label(
        "TOOLS",
        list_width,
        active=state.active_pane == "sidebar",
    )
    detail = pane_label(
        "DETAIL",
        detail_width,
        active=state.active_pane == "detail",
    )
    if html:
        return f"{tools} │ {detail}"
    return f"{fit('TOOLS', list_width)} │ {fit('DETAIL', detail_width)}"


def footer_text(
    state: ChromeState,
    visible: list[ToolInspectorItem],
    detail_line_count: int,
    body_rows: int,
) -> str:
    """Render search, notice, or focused-pane status text."""
    search = f"Search: {state.search}" if state.search else ""
    if state.searching:
        search = f"Search: {state.search}_"
    detail_start = min(detail_line_count, state.detail_scroll + 1)
    detail_end = min(detail_line_count, state.detail_scroll + body_rows)
    detail_position = f"detail {detail_start}-{detail_end}/{detail_line_count}"
    if state.active_pane == "sidebar":
        tool_count = sum(1 for item in visible if isinstance(item, ToolTraceEntry))
        summary = f"TOOLS focused · ↑/↓ move · → details · {tool_count} calls"
    else:
        summary = f"DETAIL focused · ↑/↓ scroll · ← tools · {detail_position}"
    return state.notice or search or summary
