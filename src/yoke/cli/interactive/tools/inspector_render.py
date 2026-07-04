"""Rendering helpers for the interactive tool inspector."""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Sequence
from contextlib import suppress
from html import escape as html_escape
from typing import Literal
from typing import Protocol

from yoke.cli.interactive.tools.inspector_format import (
    format_arguments,
)
from yoke.cli.interactive.tools.inspector_format import format_result
from yoke.cli.interactive.tools.inspector_format import pretty_json
from yoke.cli.interactive.tools.inspector_format import section_header
from yoke.cli.interactive.tools.trace import ToolTraceEntry
from yoke.cli.interactive.tools.trace import ToolTraceContext
from yoke.cli.render.base import format_tool_preview

DETAIL_DIM_OPEN = '<style fg="#777777">'
DETAIL_DIM_CLOSE = "</style>"


type ToolInspectorItem = ToolTraceEntry | ToolTraceContext


def escape(text: str) -> str:
    """Escape dynamic text for prompt-toolkit HTML/XML parsing."""
    return html_escape(_sanitize_xml_text(text))


def _sanitize_xml_text(text: str) -> str:
    return "".join(_xml_safe_char(char) for char in text)


def _xml_safe_char(char: str) -> str:
    codepoint = ord(char)
    if char in "\t\n\r" or 0x20 <= codepoint <= 0xD7FF:
        return char
    if 0xE000 <= codepoint <= 0xFFFD or 0x10000 <= codepoint <= 0x10FFFF:
        return char
    if codepoint <= 0x1F:
        return chr(0x2400 + codepoint)
    if codepoint == 0x7F:
        return "␡"
    return "�"


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


def render_view(
    state: ToolInspectorRenderState,
    visible: Sequence[ToolInspectorItem],
) -> list[str]:
    """Render the complete inspector view as terminal lines."""
    return _render_view(state, visible, html=False)


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
    columns, rows = terminal_size()
    columns = max(60, columns)
    body_rows = max(4, rows - 5)
    list_width = min(max(28, columns // 3), 46)
    detail_width = max(20, columns - list_width - 3)
    selected = selected_entry(state, visible)
    list_lines = _list_lines(
        state,
        visible,
        list_width,
        body_rows,
        html=html,
    )
    detail_lines = _detail_lines(selected, state, detail_width)
    max_detail_scroll = max(0, len(detail_lines) - body_rows)
    state.detail_scroll = max(0, min(state.detail_scroll, max_detail_scroll))
    detail_window = detail_lines[state.detail_scroll : state.detail_scroll + body_rows]
    footer = _footer_text(state, visible, len(detail_lines), body_rows)
    lines = [
        _escape_line(_title(columns), html),
        _pane_header(state, list_width, detail_width, html=html),
        "─" * columns,
    ]
    for index in range(body_rows):
        left = list_lines[index] if index < len(list_lines) else ""
        right = detail_window[index] if index < len(detail_window) else ""
        lines.append(
            f"{_fit_cell(left, list_width, html=html, trusted_markup=True)} │ "
            f"{_fit_cell(right, detail_width, html=html, trusted_markup=False)}"
        )
    lines.append("─" * columns)
    lines.append(_escape_line(fit(footer, columns), html))
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
    metadata = [f"id: {entry.tool_call_id}", f"status: {_status_label(entry.status)}"]
    if entry.iteration is not None:
        metadata.append(f"iteration: {entry.iteration}")
    if entry.duration_seconds is not None:
        metadata.append(f"duration: {format_duration(entry.duration_seconds)}")
    parts = [
        f"{entry.tool_name}  {_status_icon(entry.status)}",
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
        parts.extend(["", section_header("Live Output"), _format_output_chunks(entry)])
    parts.extend(["", section_header("Output"), format_result(entry.result)])
    return "\n".join(parts)


def move_selection(
    state: ToolInspectorRenderState,
    visible: Sequence[ToolInspectorItem],
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
    state: ToolInspectorRenderState,
    visible: Sequence[ToolInspectorItem],
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
    visible: Sequence[ToolInspectorItem],
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
    status = _status_icon(entry.status)
    duration = format_duration(entry.duration_seconds)
    summary = _argument_summary(entry)
    text = f"{marker} {status} {entry.tool_name} {duration} {summary}"
    if html:
        color = _sidebar_style(entry.status, state.active_pane)
        return f"<{color}>{escape(fit(text, width))}</{color}>"
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
        return f"<ansibrightblack>{escape(text)}</ansibrightblack>"
    if context.role == "assistant":
        return f"<ansiblue>{escape(text)}</ansiblue>"
    return f"<ansiwhite>{escape(text)}</ansiwhite>"


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
    lines = detail_text(entry, state).splitlines() or [""]
    if not state.wrap:
        return lines
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(
            textwrap.wrap(
                line,
                width=max(1, width),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return wrapped


def _argument_summary(entry: ToolTraceEntry) -> str:
    if not entry.raw_arguments:
        return ""
    preview = format_tool_preview(entry.tool_name, entry.raw_arguments)
    return preview.removeprefix(entry.tool_name).strip()


def _format_output_chunks(entry: ToolTraceEntry) -> str:
    parts: list[str] = []
    current_stream: str | None = None
    current_text: list[str] = []
    for chunk in entry.output_chunks or []:
        if current_stream is not None and chunk.stream != current_stream:
            parts.append(
                _format_output_chunk_group(current_stream, "".join(current_text))
            )
            current_text = []
        current_stream = chunk.stream
        current_text.append(chunk.text)
    if current_stream is not None:
        parts.append(_format_output_chunk_group(current_stream, "".join(current_text)))
    return "\n".join(part for part in parts if part)


def _format_output_chunk_group(stream: str, text: str) -> str:
    label = "STDERR" if stream == "stderr" else "STDOUT"
    body = text.rstrip("\n")
    if not body:
        return f"[{label}]"
    return f"[{label}]\n{body}"


def _status_icon(status: str) -> str:
    if status == "ok":
        return "✓"
    if status == "failed":
        return "✗"
    if status == "running":
        return "…"
    return "?"


def _sidebar_style(status: str, active_pane: str) -> str:
    if active_pane != "sidebar":
        return "ansibrightblack"
    if status == "ok":
        return "ansigreen"
    if status == "failed":
        return "ansired"
    return "ansiyellow"


def _fit_cell(
    text: str,
    width: int,
    *,
    html: bool,
    trusted_markup: bool = False,
) -> str:
    if html and trusted_markup and _is_rendered_markup(text):
        return text
    fitted = fit(text, width)
    if html:
        return _style_detail_line(fitted)
    return fitted


def _is_rendered_markup(text: str) -> bool:
    return text.startswith(
        (
            "<ansi",
            "<reverse>",
            '<style fg="',
        )
    )


def _style_detail_line(text: str) -> str:
    body = text.rstrip()
    padding = text[len(body) :]
    if body.startswith("╭─"):
        return f"<ansicyan>{escape(body)}</ansicyan>{padding}"
    if body.startswith("[STDERR]") or body.startswith("[ERROR]"):
        return f"<ansired><b>{escape(body)}</b></ansired>{padding}"
    if body.startswith("[STDOUT]") or body.startswith("[CONTENT]"):
        return f"<ansigreen><b>{escape(body)}</b></ansigreen>{padding}"
    if body.startswith("[META]"):
        return f"{DETAIL_DIM_OPEN}<b>{escape(body)}</b>{DETAIL_DIM_CLOSE}{padding}"
    if _looks_like_numbered_line(body):
        return _style_numbered_line(body, padding)
    if " │" in body:
        return _style_key_value_line(body, padding)
    if body.startswith(('"', "{", "}", "[", "]")):
        return f"{DETAIL_DIM_OPEN}{escape(body)}{DETAIL_DIM_CLOSE}{padding}"
    return _style_status_symbols(body, padding)


def _looks_like_numbered_line(text: str) -> bool:
    prefix, separator, _ = text.partition("│")
    return bool(separator) and prefix.strip().isdigit()


def _style_numbered_line(body: str, padding: str) -> str:
    number, _, value = body.partition("│")
    value = value[1:] if value.startswith(" ") else value
    return (
        f"{DETAIL_DIM_OPEN}{escape(number)}│{DETAIL_DIM_CLOSE} "
        f"{_style_status_symbols(value, '')}{padding}"
    )


def _style_key_value_line(body: str, padding: str) -> str:
    key, _, value = body.partition(" │")
    return (
        f"<ansicyan>{escape(key.rstrip())}</ansicyan>"
        f"{DETAIL_DIM_OPEN} │{DETAIL_DIM_CLOSE}"
        f"{escape(value)}{padding}"
    )


def _style_status_symbols(body: str, padding: str) -> str:
    styled = escape(body)
    styled = styled.replace("✓", "<ansigreen>✓</ansigreen>")
    styled = styled.replace("✗", "<ansired>✗</ansired>")
    styled = styled.replace("…", "<ansiyellow>…</ansiyellow>")
    return f"{styled}{padding}"


def _escape_line(text: str, html: bool) -> str:
    return escape(text) if html else text


def _status_label(status: str) -> str:
    return {
        "ok": "success",
        "failed": "failed",
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


def fit(text: str, width: int) -> str:
    """Pad or truncate text to a fixed terminal cell width."""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return f"{text[: width - 1]}…"


def _title(columns: int) -> str:
    return fit(
        "Tool Inspector - Ctrl+O  "
        "h/l panes  j/k move  g/G top/bottom  PgUp/PgDn page  "
        "/ search  r raw  w wrap  y copy  q close",
        columns,
    )


def _pane_header(
    state: ToolInspectorRenderState,
    list_width: int,
    detail_width: int,
    *,
    html: bool,
) -> str:
    tools = _pane_label(
        "TOOLS",
        list_width,
        active=state.active_pane == "sidebar",
    )
    detail = _pane_label(
        "DETAIL",
        detail_width,
        active=state.active_pane == "detail",
    )
    if html:
        return f"{tools} │ {detail}"
    return f"{fit('TOOLS', list_width)} │ {fit('DETAIL', detail_width)}"


def _pane_label(label: str, width: int, *, active: bool) -> str:
    padded = fit(f" {label} ", width)
    if active:
        return f"<reverse><ansicyan>{escape(padded)}</ansicyan></reverse>"
    return f"<ansibrightblack>{escape(padded)}</ansibrightblack>"


def _footer_text(
    state: ToolInspectorRenderState,
    visible: Sequence[ToolInspectorItem],
    detail_line_count: int,
    body_rows: int,
) -> str:
    search = f"Search: {state.search}" if state.search else ""
    if state.searching:
        search = f"Search: {state.search}_"
    detail_start = min(detail_line_count, state.detail_scroll + 1)
    detail_end = min(detail_line_count, state.detail_scroll + body_rows)
    detail_position = f"detail {detail_start}-{detail_end}/{detail_line_count}"
    if state.active_pane == "sidebar":
        tool_count = sum(1 for item in visible if isinstance(item, ToolTraceEntry))
        summary = f"TOOLS focused · j/k move · h/l details · {tool_count} calls"
    else:
        summary = f"DETAIL focused · j/k scroll · h/l tools · {detail_position}"
    return state.notice or search or summary
