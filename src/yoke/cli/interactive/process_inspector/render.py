"""Rendering helpers for the interactive command-process inspector."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from typing import Literal
from typing import Protocol

from yoke.agent.tools import CommandProcessSnapshot
from yoke.cli.interactive.tools.inspector_render import escape
from yoke.cli.interactive.tools.inspector_render import fit
from yoke.cli.interactive.tools.inspector_render import format_duration
from yoke.cli.interactive.tools.inspector_render import terminal_size


class ProcessInspectorRenderState(Protocol):
    """State attributes required by the process inspector renderer."""

    selected_index: int
    list_scroll: int
    detail_scroll: int
    wrap: bool
    notice: str
    active_pane: Literal["sidebar", "detail"]


def render_view_html(
    state: ProcessInspectorRenderState,
    processes: Sequence[CommandProcessSnapshot],
) -> str:
    """Render the complete process inspector as prompt-toolkit HTML."""
    items = list(processes)
    columns, rows = terminal_size()
    columns = max(60, columns)
    body_rows = max(4, rows - 5)
    list_width = min(max(32, columns // 3), 48)
    detail_width = max(20, columns - list_width - 3)
    process = selected_process(state, items)
    left_lines = _list_lines(state, items, list_width, body_rows)
    detail_lines = _detail_lines(process, state.wrap, detail_width)
    state.detail_scroll = max(
        0, min(state.detail_scroll, max(0, len(detail_lines) - body_rows))
    )
    detail_window = detail_lines[state.detail_scroll : state.detail_scroll + body_rows]
    lines = [
        escape(_title(columns)),
        _pane_header(state, list_width, detail_width),
        "─" * columns,
    ]
    for index in range(body_rows):
        left = (
            left_lines[index]
            if index < len(left_lines)
            else _styled_cell("", list_width)
        )
        right = detail_window[index] if index < len(detail_window) else ""
        lines.append(f"{left} │ {escape(fit(right, detail_width))}")
    lines.extend(
        [
            "─" * columns,
            escape(fit(_footer(state, items, len(detail_lines), body_rows), columns)),
        ]
    )
    return "\n".join(lines)


def detail_text(process: CommandProcessSnapshot) -> str:
    """Return readable details and retained output for one process."""
    exit_code = "-" if process.exit_code is None else str(process.exit_code)
    output_note = ""
    if process.original_output_bytes > process.retained_output_bytes:
        output_note = f" (tail of {process.original_output_bytes:,} output bytes)"
    return "\n".join(
        [
            f"Command session {process.session_id}",
            "",
            "PROCESS",
            f"status │ {process.status}",
            f"session ID │ {process.session_id}",
            f"OS PID │ {process.pid}",
            f"exit code │ {exit_code}",
            f"started │ {process.started_at.isoformat(timespec='seconds')}",
            f"elapsed │ {format_duration(process.elapsed_seconds)}",
            f"TTY │ {'yes' if process.tty else 'no'}",
            f"working directory │ {process.cwd}",
            "",
            "COMMAND",
            process.command,
            "",
            f"OUTPUT{output_note}",
            process.output_tail or "(no output)",
        ]
    )


def move_selection(
    state: ProcessInspectorRenderState,
    processes: list[CommandProcessSnapshot],
    delta: int,
) -> None:
    """Move process selection and reset detail scrolling."""
    if processes:
        state.selected_index = max(
            0, min(state.selected_index + delta, len(processes) - 1)
        )
        state.detail_scroll = 0


def selected_process(
    state: ProcessInspectorRenderState,
    processes: list[CommandProcessSnapshot],
) -> CommandProcessSnapshot | None:
    """Return the selected process snapshot, if any."""
    if not processes:
        return None
    state.selected_index = max(0, min(state.selected_index, len(processes) - 1))
    return processes[state.selected_index]


def page_step() -> int:
    """Return detail page-scroll step."""
    return max(1, terminal_size()[1] - 8)


def _list_lines(state, processes, width: int, row_count: int) -> list[str]:
    if not processes:
        return [_styled_cell("No command processes yet.", width)]
    if state.selected_index < state.list_scroll:
        state.list_scroll = state.selected_index
    if state.selected_index >= state.list_scroll + row_count:
        state.list_scroll = state.selected_index - row_count + 1
    window = processes[state.list_scroll : state.list_scroll + row_count]
    return [
        _list_line(state, process, index, width)
        for index, process in enumerate(window, start=state.list_scroll)
    ]


def _list_line(state, process, index: int, width: int) -> str:
    marker = ">" if index == state.selected_index else " "
    icon = {"running": "…", "exited": "✓", "failed": "✗"}[process.status]
    text = fit(
        f"{marker} {icon} {process.session_id} "
        f"{format_duration(process.elapsed_seconds)} {' '.join(process.command.split())}",
        width,
    )
    if state.active_pane != "sidebar":
        style = "ansibrightblack"
    else:
        style = {"running": "ansiyellow", "exited": "ansigreen", "failed": "ansired"}[
            process.status
        ]
    return _styled_cell(text, width, style=style)


def _styled_cell(text: str, width: int, *, style: str | None = None) -> str:
    fitted = fit(text, width)
    escaped = escape(fitted)
    return f"<{style}>{escaped}</{style}>" if style else escaped


def _detail_lines(process, wrap: bool, width: int) -> list[str]:
    if process is None:
        return ["No command processes have been started in this live runtime."]
    lines = detail_text(process).splitlines() or [""]
    if not wrap:
        return lines
    return [
        wrapped
        for line in lines
        for wrapped in (
            textwrap.wrap(
                line,
                width=max(1, width),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    ]


def _title(columns: int) -> str:
    return fit(
        "Process Inspector - /ps  ←/→ panes  ↑/↓ move  g/G top/bottom  "
        "PgUp/PgDn page  w wrap  y copy  q close",
        columns,
    )


def _pane_header(state, list_width: int, detail_width: int) -> str:
    def label(value: str, width: int, active: bool) -> str:
        text = escape(fit(f" {value} ", width))
        return f"<reverse>{text}</reverse>" if active else text

    return (
        f"{label('PROCESSES', list_width, state.active_pane == 'sidebar')} │ "
        f"{label('DETAIL', detail_width, state.active_pane == 'detail')}"
    )


def _footer(state, processes, detail_count: int, body_rows: int) -> str:
    if state.notice:
        return state.notice
    running = sum(process.status == "running" for process in processes)
    if state.active_pane == "sidebar":
        return (
            f"PROCESSES focused · ↑/↓ move · → details · {running} running · "
            f"{len(processes)} retained"
        )
    start = min(detail_count, state.detail_scroll + 1)
    end = min(detail_count, state.detail_scroll + body_rows)
    return f"DETAIL focused · ↑/↓ scroll · ← processes · {start}-{end}/{detail_count}"
