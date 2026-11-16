"""Helpers for the interactive MCP menu."""

# ruff: noqa: ANN202, B010, E501

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yoke.cli.backend.daemon.catalog import DaemonMcpInventory
from yoke.cli.runtime.selector.ui import select_list_item_interactive
from yoke.mcp.config import MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import GLOBAL_MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import server_supports_tool
from yoke.mcp.editing import set_persisted_mcp_server_enabled
from yoke.mcp.editing import toggle_persisted_mcp_tool


@dataclass(slots=True, frozen=True)
class McpMenuScope:
    """Where an MCP change should be applied."""

    id: str
    label: str
    description: str
    path: Path | None = None


@dataclass(slots=True, frozen=True)
class McpServerAction:
    """Action available from a selected MCP server."""

    id: str
    label: str
    description: str


@dataclass(slots=True, frozen=True)
class McpToolRow:
    """One MCP tool row in the interactive menu."""

    name: str
    description: str
    enabled: bool


def _load_mcp_tool_rows(
    *,
    inventory: DaemonMcpInventory | DaemonMcpInventory,
    server: McpServerConfig,
) -> list[McpToolRow] | str:
    if server.transport not in {"stdio", "streamable-http", "http"}:
        return f"MCP transport `{server.transport}` is not supported yet."
    try:
        return [
            McpToolRow(
                name=tool.name,
                description=tool.description,
                enabled=server_supports_tool(server, tool.name),
            )
            for tool in inventory.list_tools(server)
        ]
    except Exception as exc:
        return f"MCP error while listing {server.name}: {exc}"


def _select_mcp_scope(*, root: Path) -> McpMenuScope | None:
    scopes = (
        McpMenuScope(
            id="session",
            label="This session",
            description="Temporary; nothing is written to config.",
        ),
        McpMenuScope(
            id="repo",
            label="This repo",
            description=f"Write {root / MCP_CONFIG_RELATIVE_PATH}",
            path=root / MCP_CONFIG_RELATIVE_PATH,
        ),
        McpMenuScope(
            id="global",
            label="Globally",
            description=f"Write {Path.home() / GLOBAL_MCP_CONFIG_RELATIVE_PATH}",
            path=Path.home() / GLOBAL_MCP_CONFIG_RELATIVE_PATH,
        ),
    )
    return select_list_item_interactive(
        scopes,
        title="Apply MCP change where?",
        subtitle="Choose whether to keep changes temporary or persist them.",
        render_item=_render_scope_row,
        footer="Use Up/Down or j/k, Enter to choose, q to cancel.",
    )


def _render_scope_row(
    scope: McpMenuScope,
    _index: int,
    is_selected: bool,
    width: int,
) -> str:
    marker = ">" if is_selected else " "
    return f"{marker} {scope.label} - {scope.description}"[:width]


def _scope_for_action(action_id: str, *, root: Path) -> McpMenuScope | None:
    if action_id == "session":
        return McpMenuScope(
            id="session",
            label="This session",
            description="Temporary; nothing is written to config.",
        )
    if action_id == "repo":
        return McpMenuScope(
            id="repo",
            label="This repo",
            description=f"Write {root / MCP_CONFIG_RELATIVE_PATH}",
            path=root / MCP_CONFIG_RELATIVE_PATH,
        )
    if action_id == "global":
        return McpMenuScope(
            id="global",
            label="Globally",
            description=f"Write {Path.home() / GLOBAL_MCP_CONFIG_RELATIVE_PATH}",
            path=Path.home() / GLOBAL_MCP_CONFIG_RELATIVE_PATH,
        )
    return None


def _set_mcp_server_enabled(
    *,
    root: Path,
    scope: McpMenuScope,
    server: McpServerConfig,
    enabled: bool,
    inventory: DaemonMcpInventory | DaemonMcpInventory,
) -> None:
    if scope.id == "session":
        inventory.set_enabled(server, enabled)
        return
    if scope.path is None:
        return
    set_persisted_mcp_server_enabled(
        root=root,
        home=Path.home(),
        scope=scope.id,
        server=server,
        enabled=enabled,
    )


def _toggle_mcp_tool(
    *,
    root: Path,
    scope: McpMenuScope,
    server: McpServerConfig,
    tool_name: str,
    inventory: DaemonMcpInventory | DaemonMcpInventory,
) -> None:
    if scope.id == "session":
        inventory.toggle_tool(server, tool_name)
        return
    if scope.path is None:
        return
    toggle_persisted_mcp_tool(
        root=root,
        home=Path.home(),
        scope=scope.id,
        server=server,
        tool_name=tool_name,
    )


def _find_server(
    servers: tuple[McpServerConfig, ...],
    name: str,
) -> McpServerConfig | None:
    for server in servers:
        if server.name == name:
            return server
    return None
