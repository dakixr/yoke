"""Rendering helpers for the interactive MCP menu."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path

from yoke.cli.interactive.mcp_menu_helpers import McpServerAction
from yoke.cli.interactive.mcp_menu_helpers import McpToolRow
from yoke.cli.runtime.selector.ui import SelectorTableColumns
from yoke.mcp.config import MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import GLOBAL_MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import McpServerConfig


def _server_columns(
    servers: tuple[McpServerConfig, ...],
) -> SelectorTableColumns:
    return SelectorTableColumns(
        headers=("On", "Server", "Transport", "Source"),
        widths=(
            4,
            max(len("Server"), max(len(server.name) for server in servers)),
            max(
                len("Transport"),
                max(len(server.transport) for server in servers),
            ),
            max(
                len("Source"),
                max(len(_source_label(server, root=None)) for server in servers),
            ),
        ),
    )


def _render_server_row(
    server: McpServerConfig,
    *,
    root: Path,
    columns: SelectorTableColumns,
) -> str:
    state = "[x]" if server.enabled else "[ ]"
    return "  ".join(
        (
            state.ljust(columns.widths[0]),
            server.name.ljust(columns.widths[1]),
            server.transport.ljust(columns.widths[2]),
            _source_label(server, root=root).ljust(columns.widths[3]),
        )
    )


def _tool_columns(rows: list[McpToolRow]) -> SelectorTableColumns:
    return SelectorTableColumns(
        headers=("On", "Tool", "Description"),
        widths=(
            4,
            max(len("Tool"), max(len(row.name) for row in rows)),
            min(
                80,
                max(
                    len("Description"),
                    max(len(row.description) for row in rows),
                ),
            ),
        ),
    )


def _render_tool_row(
    row: McpToolRow,
    _index: int,
    _selected: bool,
    columns: SelectorTableColumns,
) -> str:
    state = "[x]" if row.enabled else "[ ]"
    description = " ".join(row.description.split())
    if len(description) > columns.widths[2]:
        description = description[: max(1, columns.widths[2] - 1)].rstrip() + "…"
    return "  ".join(
        (
            state.ljust(columns.widths[0]),
            row.name.ljust(columns.widths[1]),
            description.ljust(columns.widths[2]),
        )
    )


def _render_server_action_row(
    action: McpServerAction,
    _index: int,
    is_selected: bool,
    width: int,
) -> str:
    marker = ">" if is_selected else " "
    return f"{marker} {action.label} - {action.description}"[:width]


def _source_label(server: McpServerConfig, *, root: Path | None) -> str:
    if server.source_path is None:
        return "session"
    if root is not None and server.source_path == root / MCP_CONFIG_RELATIVE_PATH:
        return "repo"
    if server.source_path == Path.home() / GLOBAL_MCP_CONFIG_RELATIVE_PATH:
        return "global"
    return str(server.source_path)
