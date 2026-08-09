"""Rendering helpers for the interactive tool inspector."""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Sequence
from contextlib import suppress
from typing import Literal
from typing import Protocol

from yoke.cli.interactive.tool_inspector.chrome import footer_text
from yoke.cli.interactive.tool_inspector.chrome import pane_header
from yoke.cli.interactive.tool_inspector.chrome import title
from yoke.cli.interactive.tool_inspector.display import format_duration
from yoke.cli.interactive.tool_inspector.display import sidebar_style
from yoke.cli.interactive.tool_inspector.display import status_icon
from yoke.cli.interactive.tool_inspector.display import status_label
from yoke.cli.interactive.tool_inspector.format import format_arguments
from yoke.cli.interactive.tool_inspector.format import (
    format_output_chunks,
)
from yoke.cli.interactive.tool_inspector.format import format_result
from yoke.cli.interactive.tool_inspector.format import pretty_json
from yoke.cli.interactive.tool_inspector.format import section_header
from yoke.cli.interactive.tool_inspector.styles import escape_line
from yoke.cli.interactive.tool_inspector.styles import escape_html
from yoke.cli.interactive.tool_inspector.styles import fit
from yoke.cli.interactive.tool_inspector.styles import fit_cell
from yoke.cli.interactive.tool_inspector.trace import ToolTraceContext
from yoke.cli.interactive.tool_inspector.trace import ToolTraceEntry
from yoke.cli.render.base import format_tool_preview


type ToolInspectorItem = ToolTraceEntry | ToolTraceContext


class ToolInspectorRenderState(Protocol):
    """State attributes required by the tool inspector renderer."""

    entries: list[ToolTraceEntry]
    selected_index: int
    list_scroll: int
    detail_scroll: int
    search: str
    searching: bool
    raw: bool
    wrap: bool
    notice: str
    active_pane: Literal["sidebar", "detail"]
    data_revision: int
    detail_cache_key: tuple[object, ...] | None
    detail_cache_lines: list[str]


class SelectionState(Protocol):
    """State attributes needed for selection changes."""

    selected_index: int
    detail_scroll: int


def render_view_html(
    state: ToolInspectorRenderState,
    visible: Sequence[ToolInspectorItem],
) -> str:
    """Render the complete inspector view as prompt-toolkit HTML."""
    return "\n".join(_render_view(state, visible, html=True))


def _render_view(
    state: ToolInspectorRenderState,
    visible: Sequence[ToolInspectorItem],
    *,
    html: bool,
) -> list[str]:
    visible_items = list(visible)
    columns, rows = terminal_size()
    columns = max(60, columns)
    body_rows = max(4, rows - 5)
    list_width = min(max(28, columns // 3), 46)
    detail_width = max(20, columns - list_width - 3)
    selected = selected_entry(state, visible_items)
    list_lines = _list_lines(
        state,
        visible_items,
        list_width,
        body_rows,
        html=html,
    )
    detail_lines = _detail_lines(selected, state, detail_width)
    max_detail_scroll = max(0, len(detail_lines) - body_rows)
    state.detail_scroll = max(0, min(state.detail_scroll, max_detail_scroll))
    detail_window = detail_lines[state.detail_scroll : state.detail_scroll + body_rows]
    footer = footer_text(state, visible_items, len(detail_lines), body_rows)
    lines = [
        escape_line(title(columns), html),
        pane_header(state, list_width, detail_width, html=html),
        "─" * columns,
    ]
    for index in range(body_rows):
        left = list_lines[index] if index < len(list_lines) else ""
        right = detail_window[index] if index < len(detail_window) else ""
        lines.append(
            f"{fit_cell(left, list_width, html=html, trusted_markup=html)} │ "
            f"{fit_cell(right, detail_width, html=html)}"
        )
    lines.append("─" * columns)
    lines.append(escape_line(fit(footer, columns), html))
    return lines


def detail_text(
    entry: ToolInspectorItem,
    state: ToolInspectorRenderState,
) -> str:
    """Format one trace entry as detailed readable text."""
    if isinstance(entry, ToolTraceContext):
        return _context_detail_text(entry, state)
    payload = {
        "tool_call_id": entry.tool_call_id,
        "tool_name": entry.tool_name,
        "status": entry.status,
        "iteration": entry.iteration,
        "duration_seconds": entry.duration_seconds,
        "raw_arguments": entry.raw_arguments,
        "executed_arguments": entry.executed_arguments,
        "result": entry.result,
        "output_chunks": [
            {"stream": chunk.stream, "text": chunk.text}
            for chunk in entry.output_chunks or []
        ],
    }
    if state.raw:
        return pretty_json(payload)
    metadata = [
        f"id: {entry.tool_call_id}",
        f"status: {status_label(entry.status)}",
    ]
    if entry.iteration is not None:
        metadata.append(f"iteration: {entry.iteration}")
    if entry.duration_seconds is not None:
        metadata.append(f"duration: {format_duration(entry.duration_seconds)}")
    parts = [
        f"{entry.tool_name}  {status_icon(entry.status)}",
        " · ".join(metadata),
        "",
        section_header("Arguments"),
        format_arguments(entry.raw_arguments),
    ]
    if entry.executed_arguments is not None:
        parts.extend(
            [
                "",
                section_header("Executed Arguments"),
                pretty_json(entry.executed_arguments),
            ]
        )
    if entry.output_chunks:
        parts.extend(["", section_header("Live Output"), format_output_chunks(entry)])
    parts.extend(["", section_header("Output"), format_result(entry.result)])
    return "\n".join(parts)


def move_selection(
    state: SelectionState,
    visible: list[ToolInspectorItem],
    delta: int,
) -> None:
    """Move the selected row and reset detail scroll."""
    if not visible:
        return
    state.selected_index = max(
        0,
        min(state.selected_index + delta, len(visible) - 1),
    )
    state.detail_scroll = 0


def selected_entry(
    state: SelectionState,
    visible: list[ToolInspectorItem],
) -> ToolInspectorItem | None:
    """Return the selected visible entry, if any."""
    if not visible:
        return None
    state.selected_index = max(0, min(state.selected_index, len(visible) - 1))
    return visible[state.selected_index]


def entry_text(entry: ToolInspectorItem) -> str:
    """Return searchable text for a trace entry."""
    if isinstance(entry, ToolTraceContext):
        return f"{entry.role} {entry.text}".lower()
    return " ".join(
        str(value).lower()
        for value in (
            entry.tool_name,
            entry.tool_call_id,
            entry.raw_arguments,
            entry.executed_arguments,
            entry.result,
            "".join(chunk.text for chunk in entry.output_chunks or []),
            entry.status,
            entry.context,
        )
        if value is not None
    )


def page_step() -> int:
    """Return detail page-scroll step."""
    return max(1, terminal_size()[1] - 8)


def terminal_size() -> tuple[int, int]:
    """Return current terminal size."""
    with suppress(Exception):
        from prompt_toolkit.application.current import get_app_or_none

        app = get_app_or_none()
        if app is not None:
            size = app.output.get_size()
            return size.columns, size.rows
    size = shutil.get_terminal_size(fallback=(100, 24))
    return size.columns, size.lines


def sidebar_items(entries: list[ToolTraceEntry]) -> list[ToolInspectorItem]:
    """Return selectable sidebar items for tool entries and their context."""
    items: list[ToolInspectorItem] = []
    for entry in entries:
        if entry.context:
            items.extend(entry.context)
        items.append(entry)
        if entry.after_context:
            items.extend(entry.after_context)
    return items


def _list_lines(
    state: ToolInspectorRenderState,
    visible: list[ToolInspectorItem],
    width: int,
    row_count: int,
    *,
    html: bool = False,
) -> list[str]:
    if not visible:
        return ["No tool calls yet."]
    state.selected_index = max(0, min(state.selected_index, len(visible) - 1))
    if state.selected_index < state.list_scroll:
        state.list_scroll = state.selected_index
    if state.selected_index >= state.list_scroll + row_count:
        state.list_scroll = state.selected_index - row_count + 1
    window = visible[state.list_scroll : state.list_scroll + row_count]
    return [
        _list_line(state, entry, index, width, html=html)
        for index, entry in enumerate(window, start=state.list_scroll)
    ]


def _list_line(
    state: ToolInspectorRenderState,
    entry: ToolInspectorItem,
    index: int,
    width: int,
    *,
    html: bool = False,
) -> str:
    if isinstance(entry, ToolTraceContext):
        return _context_line(
            entry,
            index,
            state,
            width,
            html=html,
        )
    marker = ">" if index == state.selected_index else " "
    status = status_icon(entry.status)
    duration = format_duration(entry.duration_seconds)
    summary = _argument_summary(entry)
    text = f"{marker} {status} {entry.tool_name} {duration} {summary}"
    if html:
        color = sidebar_style(entry.status, state.active_pane)
        return f"<{color}>{escape_html(fit(text, width))}</{color}>"
    return fit(text, width)


def _context_line(
    context: ToolTraceContext,
    index: int,
    state: ToolInspectorRenderState,
    width: int,
    *,
    html: bool,
) -> str:
    marker = ">" if index == state.selected_index else " "
    label = "usr" if context.role == "user" else "asst"
    text = fit(f"{marker} {label} {_compact_sidebar_text(context.text)}", width)
    if not html:
        return text
    if state.active_pane != "sidebar":
        return f"<ansibrightblack>{escape_html(text)}</ansibrightblack>"
    if context.role == "assistant":
        return f"<ansiblue>{escape_html(text)}</ansiblue>"
    return f"<ansiwhite>{escape_html(text)}</ansiwhite>"


def _compact_sidebar_text(text: str) -> str:
    return " ".join(text.split())


def _context_detail_text(
    context: ToolTraceContext,
    state: ToolInspectorRenderState,
) -> str:
    payload = {"role": context.role, "content": context.text}
    if state.raw:
        return pretty_json(payload)
    title = "User Message" if context.role == "user" else "Assistant Message"
    return "\n".join([title, "", section_header("Message"), context.text or "(empty)"])


def _detail_lines(
    entry: ToolInspectorItem | None,
    state: ToolInspectorRenderState,
    width: int,
) -> list[str]:
    if entry is None:
        return ["No tool calls match the current search."]
    cache_key = (
        state.data_revision,
        id(entry),
        state.raw,
        state.wrap,
        width,
    )
    if state.detail_cache_key == cache_key:
        return state.detail_cache_lines
    lines = detail_text(entry, state).splitlines() or [""]
    if not state.wrap:
        detail_lines = lines
    else:
        detail_lines = []
        for line in lines:
            detail_lines.extend(
                textwrap.wrap(
                    line,
                    width=max(1, width),
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                or [""]
            )
    state.detail_cache_key = cache_key
    state.detail_cache_lines = detail_lines
    return detail_lines


def _argument_summary(entry: ToolTraceEntry) -> str:
    if not entry.raw_arguments:
        return ""
    preview = format_tool_preview(entry.tool_name, entry.raw_arguments)
    return preview.removeprefix(entry.tool_name).strip()
