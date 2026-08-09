"""Mouse interaction behavior for the tool inspector."""

from __future__ import annotations

from typing import Literal
from typing import Protocol

from yoke.cli.interactive.tool_inspector.render import ToolInspectorItem
from yoke.cli.interactive.tool_inspector.render import move_selection
from yoke.cli.interactive.tool_inspector.render import terminal_size


class MouseState(Protocol):
    """Inspector state used by mouse interactions."""

    active_pane: Literal["sidebar", "detail"]
    list_scroll: int
    selected_index: int
    detail_scroll: int


def handle_mouse_event(
    state: MouseState,
    visible: list[ToolInspectorItem],
    mouse_event,
) -> None:
    """Apply a prompt-toolkit mouse event to inspector state."""
    from prompt_toolkit.mouse_events import MouseEventType

    columns, rows = terminal_size()
    columns = max(60, columns)
    body_rows = max(4, rows - 5)
    list_width = min(max(28, columns // 3), 46)
    x = mouse_event.position.x
    y = mouse_event.position.y
    if x <= list_width:
        state.active_pane = "sidebar"
    elif x >= list_width + 3:
        state.active_pane = "detail"
    if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
        if state.active_pane == "sidebar":
            move_selection(state, visible, 3)
        else:
            state.detail_scroll += 3
        return
    if mouse_event.event_type == MouseEventType.SCROLL_UP:
        if state.active_pane == "sidebar":
            move_selection(state, visible, -3)
        else:
            state.detail_scroll = max(0, state.detail_scroll - 3)
        return
    if mouse_event.event_type != MouseEventType.MOUSE_UP:
        return
    row = y - 3
    if state.active_pane == "sidebar" and 0 <= row < body_rows:
        index = state.list_scroll + row
        if 0 <= index < len(visible):
            state.selected_index = index
            state.detail_scroll = 0
