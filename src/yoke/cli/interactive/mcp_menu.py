"""Interactive slash-command menu for MCP servers and tools."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from typing import cast

from yoke.agent.loop import RuntimeAgent
from yoke.cli.render.base import Console
from yoke.cli.runtime.selector.ui import SelectorTableColumns
from yoke.cli.runtime.selector.ui import select_list_item_interactive
from yoke.cli.runtime.selector.ui import select_table_item_interactive
from yoke.mcp.config import MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import GLOBAL_MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import McpSessionPolicy
from yoke.mcp.config import McpSessionServerPolicy
from yoke.mcp.config import load_mcp_config
from yoke.mcp.config import server_supports_tool
from yoke.mcp.manager import McpManager


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
    setattr(agent.provider, "_yoke_mcp_session_policy", policy)
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


def _select_mcp_server_action(server: McpServerConfig) -> McpServerAction | None:
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


def _load_mcp_tool_rows(
    *,
    root: Path,
    server: McpServerConfig,
) -> list[McpToolRow] | str:
    if server.transport != "stdio":
        return f"MCP transport `{server.transport}` is not supported yet."
    try:
        return [
            McpToolRow(
                name=tool.name,
                description=tool.description,
                enabled=server_supports_tool(server, tool.name),
            )
            for tool in _list_server_tools(root=root, server=server)
        ]
    except Exception as exc:
        return f"MCP error while listing {server.name}: {exc}"


def _list_server_tools(*, root: Path, server: McpServerConfig):
    manager = McpManager.from_paths(root=root, home=Path.home())
    try:
        return manager.list_configured_tools(server)
    finally:
        manager.close()


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
    session_policy: McpSessionPolicy,
) -> None:
    if scope.id == "session":
        existing = session_policy.servers.get(server.name)
        session_policy.servers[server.name] = McpSessionServerPolicy(
            enabled=enabled,
            enabled_tools=(existing.enabled_tools if existing else None),
            disabled_tools=(existing.disabled_tools if existing else None),
        )
        return
    if scope.path is None:
        return
    base_server = _base_mcp_server(root=root, server=server)
    payload = _load_mcp_json(scope.path)
    entry = _ensure_server_entry(payload, base_server)
    entry["enabled"] = enabled
    _write_mcp_json(scope.path, payload)


def _toggle_mcp_tool(
    *,
    root: Path,
    scope: McpMenuScope,
    server: McpServerConfig,
    tool_name: str,
    session_policy: McpSessionPolicy,
) -> None:
    if scope.id == "session":
        _toggle_session_mcp_tool(
            session_policy=session_policy,
            server=server,
            tool_name=tool_name,
        )
        return
    if scope.path is None:
        return
    base_server = _base_mcp_server(root=root, server=server)
    payload = _load_mcp_json(scope.path)
    entry = _ensure_server_entry(payload, base_server)
    _toggle_tool_entry(
        entry,
        tool_name=tool_name,
        currently_enabled=server_supports_tool(server, tool_name),
    )
    _write_mcp_json(scope.path, payload)


def _base_mcp_server(*, root: Path, server: McpServerConfig) -> McpServerConfig:
    config = load_mcp_config(root=root, home=Path.home())
    return _find_server(config.servers, server.name) or server


def _toggle_session_mcp_tool(
    *,
    session_policy: McpSessionPolicy,
    server: McpServerConfig,
    tool_name: str,
) -> None:
    existing = session_policy.servers.get(server.name)
    currently_enabled = server_supports_tool(server, tool_name)
    enabled_tools = (
        list(server.enabled_tools) if server.enabled_tools is not None else None
    )
    disabled_tools = list(server.disabled_tools)
    if currently_enabled:
        if enabled_tools is not None:
            _remove_value(enabled_tools, tool_name)
        elif tool_name not in disabled_tools:
            disabled_tools.append(tool_name)
    else:
        _remove_value(disabled_tools, tool_name)
        if enabled_tools is not None and tool_name not in enabled_tools:
            enabled_tools.append(tool_name)
    session_policy.servers[server.name] = McpSessionServerPolicy(
        enabled=existing.enabled if existing else None,
        enabled_tools=tuple(enabled_tools) if enabled_tools is not None else None,
        disabled_tools=tuple(disabled_tools),
    )


def _toggle_tool_entry(
    entry: dict[str, Any],
    *,
    tool_name: str,
    currently_enabled: bool,
) -> None:
    enabled_tools = _optional_string_list(entry.get("enabled_tools"))
    disabled_tools = _string_list(entry.get("disabled_tools"))
    if currently_enabled:
        if enabled_tools is not None:
            _remove_value(enabled_tools, tool_name)
        elif tool_name not in disabled_tools:
            disabled_tools.append(tool_name)
    else:
        _remove_value(disabled_tools, tool_name)
        if enabled_tools is not None and tool_name not in enabled_tools:
            enabled_tools.append(tool_name)
    if enabled_tools is None:
        entry.pop("enabled_tools", None)
    else:
        entry["enabled_tools"] = enabled_tools
    if disabled_tools:
        entry["disabled_tools"] = disabled_tools
    else:
        entry.pop("disabled_tools", None)


def _load_mcp_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"mcp_servers": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid MCP config `{path}`: expected a JSON object")
    servers = payload.get("mcp_servers")
    if servers is None and "mcpServers" in payload:
        servers = payload.pop("mcpServers")
        payload["mcp_servers"] = servers
    if servers is None:
        payload["mcp_servers"] = {}
    if not isinstance(payload["mcp_servers"], dict):
        raise ValueError(f"Invalid MCP config `{path}`: mcp_servers must be an object")
    return payload


def _write_mcp_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _ensure_server_entry(
    payload: dict[str, Any],
    server: McpServerConfig,
) -> dict[str, Any]:
    servers = cast(dict[str, Any], payload.setdefault("mcp_servers", {}))
    entry = servers.get(server.name)
    if not isinstance(entry, dict):
        entry = _server_config_to_json(server)
        servers[server.name] = entry
    elif "command" not in entry and server.command is not None:
        entry.update(_server_config_to_json(server))
    return cast(dict[str, Any], entry)


def _server_config_to_json(server: McpServerConfig) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "transport": server.transport,
        "enabled": server.enabled,
    }
    if server.command is not None:
        entry["command"] = server.command
    if server.args:
        entry["args"] = list(server.args)
    if server.env:
        entry["env"] = dict(server.env)
    if server.env_vars:
        entry["env_vars"] = list(server.env_vars)
    if server.cwd is not None:
        entry["cwd"] = str(server.cwd)
    if server.url is not None:
        entry["url"] = server.url
    if server.required:
        entry["required"] = server.required
    if server.startup_timeout_sec != 10.0:
        entry["startup_timeout_sec"] = server.startup_timeout_sec
    if server.tool_timeout_sec != 60.0:
        entry["tool_timeout_sec"] = server.tool_timeout_sec
    if server.enabled_tools is not None:
        entry["enabled_tools"] = list(server.enabled_tools)
    if server.disabled_tools:
        entry["disabled_tools"] = list(server.disabled_tools)
    return entry


def _optional_string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    return _string_list(value)


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected a list of strings")
    return [item for item in value if isinstance(item, str)]


def _remove_value(values: list[str], value: str) -> None:
    values[:] = [item for item in values if item != value]


def _find_server(
    servers: tuple[McpServerConfig, ...],
    name: str,
) -> McpServerConfig | None:
    for server in servers:
        if server.name == name:
            return server
    return None


def _refresh_mcp_tools(agent: RuntimeAgent) -> None:
    old_non_mcp_names = {
        name for name in agent.tools if name not in {"mcp_inspect", "mcp_call"}
    }
    agent.refresh_tools(force=True)
    agent.tools = {
        name: tool
        for name, tool in agent.tools.items()
        if name in {"mcp_inspect", "mcp_call"} or name in old_non_mcp_names
    }


def _server_columns(servers: tuple[McpServerConfig, ...]) -> SelectorTableColumns:
    return SelectorTableColumns(
        headers=("On", "Server", "Transport", "Source"),
        widths=(
            4,
            max(len("Server"), max(len(server.name) for server in servers)),
            max(len("Transport"), max(len(server.transport) for server in servers)),
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
            min(80, max(len("Description"), max(len(row.description) for row in rows))),
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


def _render_scope_row(
    scope: McpMenuScope,
    _index: int,
    is_selected: bool,
    width: int,
) -> str:
    marker = ">" if is_selected else " "
    return f"{marker} {scope.label} - {scope.description}"[:width]


def _source_label(server: McpServerConfig, *, root: Path | None) -> str:
    if server.source_path is None:
        return "session"
    if root is not None and server.source_path == root / MCP_CONFIG_RELATIVE_PATH:
        return "repo"
    if server.source_path == Path.home() / GLOBAL_MCP_CONFIG_RELATIVE_PATH:
        return "global"
    return str(server.source_path)
