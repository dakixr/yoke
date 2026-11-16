"""Interactive slash-command menu for MCP servers and tools."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from typing import cast

from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.interactive.mcp_menu_helpers import (
    McpServerAction,
    McpToolRow,
    _find_server,
    _load_mcp_tool_rows,
    _scope_for_action,
    _select_mcp_scope,
    _set_mcp_server_enabled,
    _toggle_mcp_tool,
)
from yoke.cli.interactive.mcp_menu_render import (
    _render_server_action_row,
    _render_server_row,
    _render_tool_row,
    _server_columns,
    _tool_columns,
)
from yoke.cli.render.base import Console
from yoke.cli.runtime.selector.ui import (
    select_list_item_interactive,
    select_table_item_interactive,
)
from yoke.mcp.config import (
    McpServerConfig,
    server_supports_tool,
)


def handle_mcp_menu(
    *,
    agent: DaemonSessionBackend,
    console: Console,
    root: Path,
    initial_server: str | None = None,
) -> None:
    """Open the interactive MCP server/tool menu."""
    from yoke.cli.render import print_scrollback_notice

    backend = agent
    inventory = backend.mcp(root)
    selected_server = initial_server
    while True:
        config = inventory.config()
        if not config.servers:
            print_scrollback_notice(
                console,
                "No MCP servers configured. Add one to .yoke/mcp.json or ~/.yoke/mcp.json.",
            )
            return
        if selected_server is not None:
            server = _find_server(config.servers, selected_server)
            selected_server = None
            if server is None:
                print_scrollback_notice(
                    console, f"Unknown MCP server: {initial_server}"
                )
                return
        else:
            server = _select_mcp_server(config.servers, root=root)
            if server is None:
                print_scrollback_notice(console, "MCP menu closed.")
                return

        action = _select_mcp_server_action(server)
        if action is None:
            continue
        if action.id == "tools":
            _handle_mcp_tool_menu(
                agent=backend,
                console=console,
                root=root,
                server=server,
            )
            continue
        scope = _scope_for_action(action.id, root=root)
        if scope is None:
            continue
        new_enabled = not server.enabled
        _set_mcp_server_enabled(
            root=root,
            scope=scope,
            server=server,
            enabled=new_enabled,
            inventory=inventory,
        )
        inventory.refresh()
        print_scrollback_notice(
            console,
            f"MCP server {server.name} {'enabled' if new_enabled else 'disabled'} for {scope.label.lower()}.",
        )


def _select_mcp_server(
    servers: tuple[McpServerConfig, ...],
    *,
    root: Path,
) -> McpServerConfig | None:
    return select_table_item_interactive(
        servers,
        title="MCP servers:",
        subtitle="Select a server to toggle or inspect its tools.",
        columns=_server_columns(servers),
        render_row=lambda server, _index, _selected, columns: _render_server_row(
            cast(McpServerConfig, server),
            root=root,
            columns=columns,
        ),
        footer="Use Up/Down or j/k, Enter for actions, q to close.",
    )


def _select_mcp_server_action(
    server: McpServerConfig,
) -> McpServerAction | None:
    state = "Disable" if server.enabled else "Enable"
    actions = (
        McpServerAction(
            "tools",
            "Drill into tools",
            "List this MCP's tools and toggle them individually.",
        ),
        McpServerAction(
            "session",
            f"{state} for this session",
            "Temporary; nothing is written to config.",
        ),
        McpServerAction(
            "repo",
            f"{state} for this repo",
            "Write the workspace .yoke/mcp.json file.",
        ),
        McpServerAction(
            "global",
            f"{state} globally",
            "Write the ~/.yoke/mcp.json file.",
        ),
    )
    return select_list_item_interactive(
        actions,
        title=f"MCP server: {server.name}",
        subtitle="Choose what to change.",
        render_item=_render_server_action_row,
        footer="Use Up/Down or j/k, Enter to choose, q to go back.",
    )


def _handle_mcp_tool_menu(
    *,
    agent: DaemonSessionBackend,
    console: Console,
    root: Path,
    server: McpServerConfig,
) -> None:
    from yoke.cli.render import print_scrollback_notice

    if not server.enabled:
        print_scrollback_notice(
            console,
            f"MCP server {server.name} is disabled. Enable it before drilling into tools.",
        )
        return
    inventory = agent.mcp(root)
    rows_or_error = _load_mcp_tool_rows(inventory=inventory, server=server)
    if isinstance(rows_or_error, str):
        print_scrollback_notice(console, rows_or_error)
        return
    rows = rows_or_error
    if not rows:
        print_scrollback_notice(console, f"MCP server {server.name} exposes no tools.")
        return
    while True:
        row = select_table_item_interactive(
            rows,
            title=f"MCP tools: {server.name}",
            subtitle="Select a tool to enable/disable it for a scope.",
            columns=_tool_columns(rows),
            render_row=_render_tool_row,
            footer="Use Up/Down or j/k, Enter for scopes, q to go back.",
        )
        if row is None:
            return
        scope = _select_mcp_scope(root=root)
        if scope is None:
            continue
        _toggle_mcp_tool(
            root=root,
            scope=scope,
            server=server,
            tool_name=row.name,
            inventory=inventory,
        )
        inventory.refresh()
        config = inventory.config()
        refreshed = _find_server(config.servers, server.name)
        if refreshed is not None:
            server = refreshed
            rows = [
                McpToolRow(
                    name=tool.name,
                    description=tool.description,
                    enabled=server_supports_tool(server, tool.name),
                )
                for tool in inventory.list_tools(server)
            ]
        print_scrollback_notice(
            console,
            f"MCP tool {server.name}/{row.name} toggled for {scope.label.lower()}.",
        )
