"""Rendering helpers for the interactive command-process inspector."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Literal
from typing import Protocol

from yoke.agent.tools.command_process_types import (
    CommandProcessSnapshot,
)
from yoke.cli.interactive.tool_inspector.display import format_duration
from yoke.cli.interactive.tool_inspector.render import terminal_size
from yoke.cli.interactive.tool_inspector.styles import escape_html
from yoke.cli.interactive.tool_inspector.styles import escape_line
from yoke.cli.interactive.tool_inspector.styles import fit
from yoke.cli.interactive.tool_inspector.styles import fit_cell
from yoke.cli.interactive.tool_inspector.styles import pane_label


class ProcessInspectorRenderState(Protocol):
    """State attributes required by the process inspector renderer."""

    selected_index: int
    list_scroll: int
    detail_scroll: int
    wrap: bool
    notice: str
    active_pane: Literal["sidebar", "detail"]
    detail_output_cache: DetailOutputCache


@dataclass(slots=True)
class DetailOutputCache:
    """Cache the expensive wrapping of one selected process output tail."""

    _key: tuple[int, int, datetime, int, int, bool, int] | None = None
    _lines: list[str] = field(default_factory=list)

    def lines(
        self,
        process: CommandProcessSnapshot,
        *,
        wrap: bool,
        width: int,
    ) -> list[str]:
        """Return cached output lines for an unchanged process snapshot."""
        key = (
            process.session_id,
            process.pid,
            process.started_at,
            process.original_output_bytes,
            process.retained_output_bytes,
            wrap,
            width,
        )
        if key != self._key:
            self._key = key
            self._lines = _wrapped_lines(
                process.output_tail or "(no output)",
                wrap=wrap,
                width=width,
            )
        return self._lines


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
    detail_lines = _detail_lines(
        process,
        state.wrap,
        detail_width,
        state.detail_output_cache,
    )
    max_scroll = max(0, len(detail_lines) - body_rows)
    state.detail_scroll = max(0, min(state.detail_scroll, max_scroll))
    detail_window = detail_lines[state.detail_scroll : state.detail_scroll + body_rows]
    lines = [
        escape_line(_title(columns), True),
        _pane_header(state, list_width, detail_width),
        "─" * columns,
    ]
    for index in range(body_rows):
        left = left_lines[index] if index < len(left_lines) else ""
        right = detail_window[index] if index < len(detail_window) else ""
        lines.append(
            f"{fit_cell(left, list_width, html=True, trusted_markup=True)} │ "
            f"{fit_cell(right, detail_width, html=True)}"
        )
    lines.append("─" * columns)
    lines.append(
        escape_line(
            fit(_footer(state, items, len(detail_lines), body_rows), columns),
            True,
        )
    )
    return "\n".join(lines)


def detail_text(process: CommandProcessSnapshot) -> str:
    """Return readable details and output for one process."""
    output = process.output_tail or "(no output)"
    return "\n".join([*_detail_metadata_lines(process), output])


def move_selection(
    state: ProcessInspectorRenderState,
    processes: list[CommandProcessSnapshot],
    delta: int,
) -> None:
    """Move process selection and reset detail scrolling."""
    if not processes:
        return
    state.selected_index = max(0, min(state.selected_index + delta, len(processes) - 1))
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


def _list_lines(
    state: ProcessInspectorRenderState,
    processes: list[CommandProcessSnapshot],
    width: int,
    row_count: int,
) -> list[str]:
    if not processes:
        return ["No command processes yet."]
    state.selected_index = max(0, min(state.selected_index, len(processes) - 1))
    if state.selected_index < state.list_scroll:
        state.list_scroll = state.selected_index
    if state.selected_index >= state.list_scroll + row_count:
        state.list_scroll = state.selected_index - row_count + 1
    window = processes[state.list_scroll : state.list_scroll + row_count]
    return [
        _list_line(state, process, index, width)
        for index, process in enumerate(window, start=state.list_scroll)
    ]


def _list_line(
    state: ProcessInspectorRenderState,
    process: CommandProcessSnapshot,
    index: int,
    width: int,
) -> str:
    marker = ">" if index == state.selected_index else " "
    icon = {"running": "…", "exited": "✓", "failed": "✗"}[process.status]
    command = " ".join(process.command.split())
    text = fit(
        f"{marker} {icon} {process.session_id} "
        f"{format_duration(process.elapsed_seconds)} {command}",
        width,
    )
    style = _sidebar_style(process.status, state.active_pane)
    return f"<{style}>{escape_html(text)}</{style}>"


def _sidebar_style(status: str, active_pane: str) -> str:
    if active_pane != "sidebar":
        return "ansibrightblack"
    if status == "exited":
        return "ansigreen"
    if status == "failed":
        return "ansired"
    return "ansiyellow"


def _detail_lines(
    process: CommandProcessSnapshot | None,
    wrap: bool,
    width: int,
    output_cache: DetailOutputCache,
) -> list[str]:
    if process is None:
        return ["No command processes have been started in this live runtime."]
    output_lines = output_cache.lines(process, wrap=wrap, width=width)
    lines = _detail_metadata_lines(process)
    if not wrap:
        return [*lines, *output_lines]
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
    wrapped.extend(output_lines)
    return wrapped


def _detail_metadata_lines(process: CommandProcessSnapshot) -> list[str]:
    """Return the small metadata prefix rendered above cached output."""
    exit_code = "-" if process.exit_code is None else str(process.exit_code)
    output_note = ""
    if process.original_output_bytes > process.retained_output_bytes:
        output_note = f" (tail of {process.original_output_bytes:,} output bytes)"
    return [
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
    ]


def _wrapped_lines(text: str, *, wrap: bool, width: int) -> list[str]:
    """Split and optionally wrap output text."""
    lines = text.splitlines() or [""]
    if not wrap:
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


def _title(columns: int) -> str:
    return fit(
        "Process Inspector - /ps  ←/→ panes  ↑/↓ move  "
        "g/G top/bottom  PgUp/PgDn page  w wrap  y copy  q close",
        columns,
    )


def _pane_header(
    state: ProcessInspectorRenderState,
    list_width: int,
    detail_width: int,
) -> str:
    processes = pane_label(
        "PROCESSES", list_width, active=state.active_pane == "sidebar"
    )
    detail = pane_label("DETAIL", detail_width, active=state.active_pane == "detail")
    return f"{processes} │ {detail}"


def _footer(
    state: ProcessInspectorRenderState,
    processes: list[CommandProcessSnapshot],
    detail_line_count: int,
    body_rows: int,
) -> str:
    if state.notice:
        return state.notice
    running = sum(process.status == "running" for process in processes)
    if state.active_pane == "sidebar":
        return (
            f"PROCESSES focused · ↑/↓ move · → details · "
            f"{running} running · {len(processes)} retained"
        )
    start = min(detail_line_count, state.detail_scroll + 1)
    end = min(detail_line_count, state.detail_scroll + body_rows)
    return (
        f"DETAIL focused · ↑/↓ scroll · ← processes · {start}-{end}/{detail_line_count}"
    )
