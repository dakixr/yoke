"""Helpers for the interactive MCP menu."""

# ruff: noqa: ANN202, B010, E501

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from typing import cast

from yoke.agent.loop.agent import RuntimeAgent
from yoke.cli.runtime.selector.ui import select_list_item_interactive
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


def _load_mcp_tool_rows(
    *,
    root: Path,
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
    if not server.verify:
        entry["verify"] = False
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
