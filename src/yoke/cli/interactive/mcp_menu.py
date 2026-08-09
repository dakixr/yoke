"""Interactive slash-command menu for MCP servers and tools."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from typing import cast

from yoke.agent.loop.agent import RuntimeAgent
from yoke.cli.render.base import Console
from yoke.cli.runtime.selector.ui import select_list_item_interactive
from yoke.cli.runtime.selector.ui import select_table_item_interactive
from yoke.cli.interactive.mcp_menu_helpers import McpServerAction
from yoke.cli.interactive.mcp_menu_helpers import McpToolRow
from yoke.cli.interactive.mcp_menu_helpers import _find_server
from yoke.cli.interactive.mcp_menu_helpers import _list_server_tools
from yoke.cli.interactive.mcp_menu_helpers import _load_mcp_tool_rows
from yoke.cli.interactive.mcp_menu_helpers import _refresh_mcp_tools
from yoke.cli.interactive.mcp_menu_helpers import _scope_for_action
from yoke.cli.interactive.mcp_menu_helpers import _select_mcp_scope
from yoke.cli.interactive.mcp_menu_helpers import (
    _set_mcp_server_enabled,
)
from yoke.cli.interactive.mcp_menu_helpers import _toggle_mcp_tool
from yoke.cli.interactive.mcp_menu_render import (
    _render_server_action_row,
)
from yoke.cli.interactive.mcp_menu_render import _render_server_row
from yoke.cli.interactive.mcp_menu_render import _render_tool_row
from yoke.cli.interactive.mcp_menu_render import _server_columns
from yoke.cli.interactive.mcp_menu_render import _tool_columns
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import McpSessionPolicy
from yoke.mcp.config import load_mcp_config
from yoke.mcp.config import server_supports_tool


def handle_mcp_menu(
    *,
    agent: object,
    console: Console,
    root: Path,
    initial_server: str | None = None,
) -> None:
    """Open the interactive MCP server/tool menu."""
    from yoke.cli.render import print_scrollback_notice

    if not isinstance(agent, RuntimeAgent):
        print_scrollback_notice(
            console, "/mcp is only available for RuntimeAgent sessions."
        )
        return
    session_policy = ensure_mcp_session_policy(agent)
    selected_server = initial_server
    while True:
        config = load_mcp_config(
            root=root,
            home=Path.home(),
            session_policy=session_policy,
        )
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
                agent=agent,
                console=console,
                root=root,
                server=server,
                session_policy=session_policy,
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
            session_policy=session_policy,
        )
        _refresh_mcp_tools(agent)
        print_scrollback_notice(
            console,
            f"MCP server {server.name} {'enabled' if new_enabled else 'disabled'} for {scope.label.lower()}.",
        )


def ensure_mcp_session_policy(agent: RuntimeAgent) -> McpSessionPolicy:
    """Return the session MCP policy attached to this agent/provider."""
    existing = getattr(agent.provider, "_yoke_mcp_session_policy", None)
    if isinstance(existing, McpSessionPolicy):
        return existing
    policy = McpSessionPolicy.empty()
    object.__setattr__(agent.provider, "_yoke_mcp_session_policy", policy)
    return policy


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
    agent: RuntimeAgent,
    console: Console,
    root: Path,
    server: McpServerConfig,
    session_policy: McpSessionPolicy,
) -> None:
    from yoke.cli.render import print_scrollback_notice

    if not server.enabled:
        print_scrollback_notice(
            console,
            f"MCP server {server.name} is disabled. Enable it before drilling into tools.",
        )
        return
    rows_or_error = _load_mcp_tool_rows(root=root, server=server)
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
            session_policy=session_policy,
        )
        _refresh_mcp_tools(agent)
        config = load_mcp_config(
            root=root,
            home=Path.home(),
            session_policy=session_policy,
        )
        refreshed = _find_server(config.servers, server.name)
        if refreshed is not None:
            server = refreshed
            rows = [
                McpToolRow(
                    name=tool.name,
                    description=tool.description,
                    enabled=server_supports_tool(server, tool.name),
                )
                for tool in _list_server_tools(root=root, server=server)
            ]
        print_scrollback_notice(
            console,
            f"MCP tool {server.name}/{row.name} toggled for {scope.label.lower()}.",
        )
