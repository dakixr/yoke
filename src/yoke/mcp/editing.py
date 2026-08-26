"""Shared MCP configuration mutations used by CLI and HTTP clients."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from typing import cast

from yoke.mcp.config import GLOBAL_MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import MCP_CONFIG_RELATIVE_PATH
from yoke.mcp.config import McpServerConfig
from yoke.mcp.config import load_mcp_config
from yoke.mcp.config import server_supports_tool


def persisted_mcp_scope_path(
    *,
    root: Path,
    home: Path,
    scope: str,
) -> Path:
    """Return the config file used by one persistent MCP mutation scope."""
    if scope == "repo":
        return root.resolve() / MCP_CONFIG_RELATIVE_PATH
    if scope == "global":
        return home.resolve() / GLOBAL_MCP_CONFIG_RELATIVE_PATH
    raise ValueError("Persistent MCP scope must be `repo` or `global`.")


def patch_persisted_mcp_server(
    *,
    root: Path,
    home: Path,
    scope: str,
    server: McpServerConfig,
    enabled: bool | None = None,
    enabled_tools: tuple[str, ...] | None = None,
    disabled_tools: tuple[str, ...] | None = None,
    update_enabled_tools: bool = False,
    update_disabled_tools: bool = False,
) -> McpServerConfig:
    """Persist selected MCP server policy fields and return effective config."""
    path = persisted_mcp_scope_path(root=root, home=home, scope=scope)
    base_server = _base_mcp_server(root=root, home=home, server=server)
    payload = _load_mcp_json(path)
    entry = _ensure_server_entry(payload, base_server)
    if enabled is not None:
        entry["enabled"] = enabled
    if update_enabled_tools:
        if enabled_tools is None:
            entry.pop("enabled_tools", None)
        else:
            entry["enabled_tools"] = list(enabled_tools)
    if update_disabled_tools:
        if disabled_tools:
            entry["disabled_tools"] = list(disabled_tools)
        else:
            entry.pop("disabled_tools", None)
    _write_mcp_json(path, payload)
    updated = load_mcp_config(root=root, home=home)
    return _find_server(updated.servers, server.name) or server


def set_persisted_mcp_server_enabled(
    *,
    root: Path,
    home: Path,
    scope: str,
    server: McpServerConfig,
    enabled: bool,
) -> McpServerConfig:
    """Persist one server enabled flag in repo or global MCP config."""
    return patch_persisted_mcp_server(
        root=root,
        home=home,
        scope=scope,
        server=server,
        enabled=enabled,
    )


def toggle_persisted_mcp_tool(
    *,
    root: Path,
    home: Path,
    scope: str,
    server: McpServerConfig,
    tool_name: str,
) -> McpServerConfig:
    """Toggle one tool using the same allow/deny semantics as the CLI menu."""
    path = persisted_mcp_scope_path(root=root, home=home, scope=scope)
    base_server = _base_mcp_server(root=root, home=home, server=server)
    payload = _load_mcp_json(path)
    entry = _ensure_server_entry(payload, base_server)
    _toggle_tool_entry(
        entry,
        tool_name=tool_name,
        currently_enabled=server_supports_tool(server, tool_name),
    )
    _write_mcp_json(path, payload)
    updated = load_mcp_config(root=root, home=home)
    return _find_server(updated.servers, server.name) or server


def _base_mcp_server(
    *,
    root: Path,
    home: Path,
    server: McpServerConfig,
) -> McpServerConfig:
    config = load_mcp_config(root=root, home=home)
    return _find_server(config.servers, server.name) or server


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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


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
