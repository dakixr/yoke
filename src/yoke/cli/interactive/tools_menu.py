"""Interactive slash-command menu for session-local tool toggles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.http.models.tool import ToolInfo
from yoke.cli.render.base import Console
from yoke.cli.runtime.selector.multiselect import (
    select_table_items_interactive,
)
from yoke.cli.runtime.selector.ui import (
    SelectorTableColumns,
    select_list_item_interactive,
)
from yoke.cli.tools.policy import PiConfig, ToolPolicy, update_config_file


@dataclass(slots=True, frozen=True)
class ToolChangeScope:
    """Where selected tool changes should be applied."""

    id: str
    label: str
    description: str


@dataclass(slots=True, frozen=True)
class ToolMenuRow:
    """One row in the interactive tools menu."""

    tools: tuple[ToolInfo, ...]
    policy_kind: str
    policy_key: str

    @property
    def name(self) -> str:
        """Return the tool name."""
        names = [tool.name for tool in self.tools]
        return ", ".join(names)

    @property
    def tool_names(self) -> set[str]:
        """Return concrete tool names controlled by this row."""
        return {tool.name for tool in self.tools}

    @property
    def source(self) -> str:
        """Return the tool source kind."""
        return self.tools[0].source

    @property
    def location(self) -> str:
        """Return a displayable source location."""
        if self.tools[0].source_path is None:
            return "builtin"
        return str(self.tools[0].source_path)


def handle_tools_menu(
    *,
    agent: DaemonSessionBackend,
    console: Console,
    root: Path | None = None,
) -> None:
    """Open the fullscreen tools menu and apply selected changes."""
    from yoke.cli.render import print_scrollback_notice

    backend = agent
    inventory = backend.tools()
    report = inventory.report()
    rows = _tool_rows(report)
    if not rows:
        print_scrollback_notice(console, "No tools are available.")
        return

    active_names = inventory.active_names()
    visible_names = {name for row in rows for name in row.tool_names}
    active_visible_names = active_names & visible_names
    selected_indexes = {
        index for index, row in enumerate(rows) if row.tool_names & active_visible_names
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

    selected_visible_names = {
        tool_name for index in result for tool_name in rows[index].tool_names
    }
    if selected_visible_names == active_visible_names:
        print_scrollback_notice(console, "No tool changes applied.")
        return

    scope = _select_tool_change_scope(root=root)
    if scope is None:
        print_scrollback_notice(console, "Tool changes cancelled.")
        return

    if scope.id != "session":
        config_path = _tool_scope_config_path(scope=scope, root=root)
        if config_path is not None:
            _write_tool_policy_config(
                config_path,
                rows=rows,
                active_names=selected_visible_names,
            )
    inventory.select(
        selected_visible_names,
        visible_names,
    )
    effective_names = selected_visible_names | (active_names - visible_names)
    print_scrollback_notice(
        console,
        _format_tool_change_summary(
            before=active_names,
            after=effective_names,
            scope=scope,
        ),
    )


def _select_tool_change_scope(
    *,
    root: Path | None,
) -> ToolChangeScope | None:
    home = Path.home()
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
            description=f"Write {home / '.yoke' / 'config.json'}",
        ),
    ]
    if root is None or root.resolve() == home.resolve():
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
    def set_policies(config: PiConfig) -> PiConfig:
        for row in rows:
            policy = (
                ToolPolicy.allow if row.tool_names & active_names else ToolPolicy.deny
            )
            if row.policy_kind == "capability":
                config.capabilities[row.policy_key] = policy
                for tool_name in row.tool_names:
                    config.tools.pop(tool_name, None)
            else:
                config.tools[row.policy_key] = policy
        return config

    update_config_file(path, set_policies, migrate_legacy=True)


def _tool_rows(tools: list[ToolInfo]) -> list[ToolMenuRow]:
    by_key: dict[tuple[str, str], list[ToolInfo]] = {}
    for tool in tools:
        if tool.source == "default" and tool.capability_id:
            key = ("capability", tool.capability_id)
        else:
            key = ("tool", tool.name)
        by_key.setdefault(key, []).append(tool)
    return [
        ToolMenuRow(
            tools=tuple(tools),
            policy_kind=kind,
            policy_key=key,
        )
        for (kind, key), tools in sorted(
            by_key.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                item[1][0].source,
                str(item[1][0].source_path or ""),
            ),
        )
    ]


def _tool_menu_columns(rows: list[ToolMenuRow]) -> SelectorTableColumns:
    return SelectorTableColumns(
        headers=("On", "Target", "Source", "Location"),
        widths=(
            4,
            max(len("Target"), max(len(row.policy_key) for row in rows)),
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
            row.policy_key.ljust(columns.widths[1]),
            row.source.ljust(columns.widths[2]),
            location.ljust(columns.widths[3]),
        )
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
