"""Interactive slash-command menu for session-local tool toggles."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.tool_core import index_tools
from yoke.cli.bootstrap.types import LoadedTool
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.render.base import Console
from yoke.cli.runtime.selector_ui import select_list_item_interactive
from yoke.cli.runtime.selector_multiselect import (
    select_table_items_interactive,
)
from yoke.cli.runtime.selector_ui import SelectorTableColumns
from yoke.cli.tools.policy import PiConfig
from yoke.cli.tools.policy import ToolPolicy
from yoke.cli.tools.policy import load_config_file


@dataclass(slots=True, frozen=True)
class ToolChangeScope:
    """Where selected tool changes should be applied."""

    id: str
    label: str
    description: str


@dataclass(slots=True, frozen=True)
class ToolMenuRow:
    """One row in the interactive tools menu."""

    loaded_tool: LoadedTool

    @property
    def name(self) -> str:
        """Return the tool name."""
        return self.loaded_tool.tool.name

    @property
    def source(self) -> str:
        """Return the tool source kind."""
        return self.loaded_tool.source_kind

    @property
    def location(self) -> str:
        """Return a displayable source location."""
        if self.loaded_tool.source_path is None:
            return "builtin"
        return str(self.loaded_tool.source_path)


def handle_tools_menu(
    *,
    agent: object,
    console: Console,
    root: Path | None = None,
) -> None:
    """Open the fullscreen tools menu and apply selected changes."""
    from yoke.cli.render import print_scrollback_notice

    if not isinstance(agent, RuntimeAgent):
        print_scrollback_notice(
            console, "/tools is only available for RuntimeAgent sessions."
        )
        return
    report = getattr(agent, "tool_report", None)
    if not isinstance(report, ToolLoadReport):
        print_scrollback_notice(console, "No tool inventory is available.")
        return

    rows = _tool_rows(report)
    if not rows:
        print_scrollback_notice(console, "No tools are available.")
        return

    active_names = set(agent.tools)
    visible_names = {row.name for row in rows}
    visible_active_names = active_names & visible_names
    selected_indexes = {
        index for index, row in enumerate(rows) if row.name in active_names
    }
    result = select_table_items_interactive(
        rows,
        title="Toggle tools for this run:",
        subtitle="Session-only changes. Nothing is written to config.",
        columns=_tool_menu_columns(rows),
        render_row=_render_tool_row,
        selected_indexes=selected_indexes,
        footer=(
            "Space toggles, a enables all, d disables all, Enter applies, q cancels."
        ),
    )
    if result is None:
        print_scrollback_notice(console, "Tool changes cancelled.")
        return

    new_names = {rows[index].name for index in result}
    if new_names == visible_active_names:
        print_scrollback_notice(console, "No tool changes applied.")
        return

    scope = _select_tool_change_scope(root=root)
    if scope is None:
        print_scrollback_notice(console, "Tool changes cancelled.")
        return

    _apply_session_tool_changes(
        agent=agent,
        report=report,
        rows=rows,
        active_names=new_names,
    )
    if scope.id != "session":
        config_path = _tool_scope_config_path(scope=scope, root=root)
        assert config_path is not None
        _write_tool_policy_config(
            config_path,
            rows=rows,
            active_names=new_names,
        )
    print_scrollback_notice(
        console,
        _format_tool_change_summary(
            before=visible_active_names,
            after=new_names,
            scope=scope,
        ),
    )


def _select_tool_change_scope(
    *,
    root: Path | None,
) -> ToolChangeScope | None:
    scopes = [
        ToolChangeScope(
            id="session",
            label="This session",
            description="Current behavior; nothing is written to config.",
        ),
        ToolChangeScope(
            id="root",
            label="This root path",
            description=(
                f"Write {root / '.yoke' / 'config.json'}"
                if root is not None
                else "Unavailable because no root path is active."
            ),
        ),
        ToolChangeScope(
            id="global",
            label="Globally",
            description=f"Write {Path.home() / '.yoke' / 'config.json'}",
        ),
    ]
    if root is None:
        scopes = [scope for scope in scopes if scope.id != "root"]
    return select_list_item_interactive(
        scopes,
        title="Apply tool changes where?",
        subtitle="Choose whether to keep changes temporary or persist them.",
        render_item=_render_tool_scope_row,
        footer="Use Up/Down or j/k, Enter to choose, q to cancel.",
    )


def _render_tool_scope_row(
    scope: ToolChangeScope,
    _index: int,
    is_selected: bool,
    width: int,
) -> str:
    marker = ">" if is_selected else " "
    text = f"{marker} {scope.label} - {scope.description}"
    return text[:width]


def _apply_session_tool_changes(
    *,
    agent: RuntimeAgent,
    report: ToolLoadReport,
    rows: list[ToolMenuRow],
    active_names: set[str],
) -> None:
    visible_names = {row.name for row in rows}
    selected_tools = [row.loaded_tool.tool for row in rows if row.name in active_names]
    hidden_runtime_tools = [
        tool for name, tool in agent.tools.items() if name not in visible_names
    ]
    selected_tools.extend(hidden_runtime_tools)
    agent.tools = index_tools(selected_tools)
    agent.tool_report = _tool_report_with_active_names(report, active_names)


def _tool_scope_config_path(
    *,
    scope: ToolChangeScope,
    root: Path | None,
) -> Path | None:
    if scope.id == "root":
        if root is None:
            return None
        return root / ".yoke" / "config.json"
    if scope.id == "global":
        return Path.home() / ".yoke" / "config.json"
    return None


def _write_tool_policy_config(
    path: Path,
    *,
    rows: list[ToolMenuRow],
    active_names: set[str],
) -> None:
    loaded_config = load_config_file(path)
    tools = dict(loaded_config.config.tools)
    for row in rows:
        tools[row.name] = (
            ToolPolicy.allow if row.name in active_names else ToolPolicy.deny
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            PiConfig(
                tools=tools,
                default_model=loaded_config.config.default_model,
                default_reasoning_effort=(
                    loaded_config.config.default_reasoning_effort
                ),
            ).model_dump(mode="json", exclude_none=True),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _tool_rows(report: ToolLoadReport) -> list[ToolMenuRow]:
    by_name: dict[str, LoadedTool] = {}
    for loaded in report.discovered_tools:
        by_name.setdefault(loaded.tool.name, loaded)
    return [
        ToolMenuRow(loaded_tool=loaded)
        for loaded in sorted(
            by_name.values(),
            key=lambda item: (
                item.tool.name,
                item.source_kind,
                str(item.source_path or ""),
            ),
        )
    ]


def _tool_menu_columns(rows: list[ToolMenuRow]) -> SelectorTableColumns:
    return SelectorTableColumns(
        headers=("On", "Tool", "Source", "Location"),
        widths=(
            4,
            max(len("Tool"), max(len(row.name) for row in rows)),
            max(len("Source"), max(len(row.source) for row in rows)),
            max(
                len("Location"),
                min(50, max(len(row.location) for row in rows)),
            ),
        ),
    )


def _render_tool_row(
    row: ToolMenuRow,
    _index: int,
    _is_cursor: bool,
    is_enabled: bool,
    columns: SelectorTableColumns,
) -> str:
    state = "[x]" if is_enabled else "[ ]"
    location = row.location
    if len(location) > columns.widths[3]:
        location = "..." + location[-max(1, columns.widths[3] - 3) :]
    return "  ".join(
        (
            state.ljust(columns.widths[0]),
            row.name.ljust(columns.widths[1]),
            row.source.ljust(columns.widths[2]),
            location.ljust(columns.widths[3]),
        )
    )


def _tool_report_with_active_names(
    report: ToolLoadReport,
    active_names: set[str],
) -> ToolLoadReport:
    return ToolLoadReport(
        discovered_tools=list(report.discovered_tools),
        active_tools=[
            entry
            for entry in report.discovered_tools
            if entry.tool.name in active_names
        ],
        denied_tools=[
            entry
            for entry in report.discovered_tools
            if entry.tool.name not in active_names
        ],
        config_path=report.config_path,
        unmatched_config_patterns=report.unmatched_config_patterns,
    )


def _format_tool_change_summary(
    *,
    before: set[str],
    after: set[str],
    scope: ToolChangeScope | None = None,
) -> str:
    enabled = sorted(after - before)
    disabled = sorted(before - after)
    parts: list[str] = []
    if enabled:
        parts.append("enabled " + ", ".join(enabled))
    if disabled:
        parts.append("disabled " + ", ".join(disabled))
    scope_label = scope.label.lower() if scope is not None else "session"
    return f"Updated tools for {scope_label}: " + "; ".join(parts)
