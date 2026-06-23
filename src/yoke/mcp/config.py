"""MCP configuration loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any
from typing import cast


MCP_CONFIG_RELATIVE_PATH = Path(".yoke") / "mcp.json"
GLOBAL_MCP_CONFIG_RELATIVE_PATH = Path(".yoke") / "mcp.json"
SUPPORTED_TRANSPORTS = {"stdio", "streamable-http"}


@dataclass(slots=True, frozen=True)
class McpServerConfig:
    """Configuration for one MCP server."""

    name: str
    transport: str = "stdio"
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    env_vars: tuple[str, ...] = ()
    cwd: Path | None = None
    url: str | None = None
    required: bool = False
    startup_timeout_sec: float = 10.0
    tool_timeout_sec: float = 60.0
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    headers: dict[str, str] | None = None
    source_path: Path | None = None


@dataclass(slots=True, frozen=True)
class McpConfig:
    """Merged MCP configuration."""

    servers: tuple[McpServerConfig, ...]
    paths: tuple[Path, ...] = ()

    @property
    def enabled_servers(self) -> tuple[McpServerConfig, ...]:
        """Return enabled servers."""
        return tuple(server for server in self.servers if server.enabled)


@dataclass(slots=True, frozen=True)
class McpSessionServerPolicy:
    """Session-local MCP server/tool policy."""

    enabled: bool | None = None
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] | None = None


@dataclass(slots=True)
class McpSessionPolicy:
    """Mutable MCP policy overrides for one interactive session."""

    servers: dict[str, McpSessionServerPolicy]

    @classmethod
    def empty(cls) -> McpSessionPolicy:
        """Return an empty session policy."""
        return cls(servers={})


def load_mcp_config(
    *,
    root: Path,
    home: Path,
    session_policy: McpSessionPolicy | None = None,
) -> McpConfig:
    """Load and merge global and workspace MCP config files."""
    resolved_home = home.resolve()
    resolved_root = root.resolve()
    candidates = (
        resolved_home / GLOBAL_MCP_CONFIG_RELATIVE_PATH,
        resolved_root / MCP_CONFIG_RELATIVE_PATH,
    )
    merged: dict[str, McpServerConfig] = {}
    loaded_paths: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        parsed = _load_config_file(path, root=resolved_root)
        loaded_paths.append(path)
        for server in parsed:
            merged[server.name] = server
    servers = tuple(merged[name] for name in sorted(merged))
    if session_policy is not None:
        servers = apply_mcp_session_policy(servers, session_policy)
    return McpConfig(
        servers=servers,
        paths=tuple(loaded_paths),
    )


def apply_mcp_session_policy(
    servers: tuple[McpServerConfig, ...],
    policy: McpSessionPolicy,
) -> tuple[McpServerConfig, ...]:
    """Apply session-local MCP overrides to server configs."""
    patched: list[McpServerConfig] = []
    for server in servers:
        server_policy = policy.servers.get(server.name)
        if server_policy is None:
            patched.append(server)
            continue
        patched.append(
            replace(
                server,
                enabled=(
                    server.enabled
                    if server_policy.enabled is None
                    else server_policy.enabled
                ),
                enabled_tools=(
                    server.enabled_tools
                    if server_policy.enabled_tools is None
                    else server_policy.enabled_tools
                ),
                disabled_tools=(
                    server.disabled_tools
                    if server_policy.disabled_tools is None
                    else server_policy.disabled_tools
                ),
            )
        )
    return tuple(patched)


def _load_config_file(path: Path, *, root: Path) -> tuple[McpServerConfig, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid MCP config `{path}`: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read MCP config `{path}`: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid MCP config `{path}`: expected a JSON object")
    servers = payload.get("mcp_servers")
    if servers is None:
        servers = payload.get("mcpServers")
    if servers is None:
        return ()
    if not isinstance(servers, dict):
        raise ValueError(f"Invalid MCP config `{path}`: mcp_servers must be an object")
    return tuple(
        _parse_server(name, value, source_path=path, root=root)
        for name, value in sorted(servers.items())
    )


def _parse_server(
    name: object,
    value: object,
    *,
    source_path: Path,
    root: Path,
) -> McpServerConfig:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"Invalid MCP config `{source_path}`: server names must be non-empty"
        )
    if not isinstance(value, dict):
        raise ValueError(
            f"Invalid MCP config `{source_path}`: server `{name}` must be an object"
        )
    server_name = name.strip()
    transport = _string(value.get("transport"), default=None)
    if transport is None:
        transport = "streamable-http" if value.get("url") is not None else "stdio"
    command = _string(value.get("command"), default=None)
    url = _string(value.get("url"), default=None)
    if transport not in SUPPORTED_TRANSPORTS:
        return McpServerConfig(
            name=server_name,
            transport=transport,
            enabled=_bool(value.get("enabled"), default=True),
            command=command,
            args=_string_tuple(value.get("args")),
            env=_string_dict(value.get("env")),
            env_vars=_string_tuple(value.get("env_vars")),
            cwd=_path(value.get("cwd"), source_path=source_path, root=root),
            url=url,
            required=_bool(value.get("required"), default=False),
            startup_timeout_sec=_positive_float(
                value.get("startup_timeout_sec"), default=10.0
            ),
            tool_timeout_sec=_positive_float(
                value.get("tool_timeout_sec"), default=60.0
            ),
            enabled_tools=_optional_string_tuple(value.get("enabled_tools")),
            disabled_tools=_string_tuple(value.get("disabled_tools")),
            headers=_string_dict(value.get("headers")),
            source_path=source_path,
        )
    if transport == "stdio" and command is None:
        raise ValueError(
            f"Invalid MCP config `{source_path}`: stdio server `{server_name}` needs command"
        )
    return McpServerConfig(
        name=server_name,
        transport=transport,
        enabled=_bool(value.get("enabled"), default=True),
        command=command,
        args=_string_tuple(value.get("args")),
        env=_string_dict(value.get("env")),
        env_vars=_string_tuple(value.get("env_vars")),
        cwd=_path(value.get("cwd"), source_path=source_path, root=root),
        url=url,
        required=_bool(value.get("required"), default=False),
        startup_timeout_sec=_positive_float(
            value.get("startup_timeout_sec"), default=10.0
        ),
        tool_timeout_sec=_positive_float(value.get("tool_timeout_sec"), default=60.0),
        enabled_tools=_optional_string_tuple(value.get("enabled_tools")),
        disabled_tools=_string_tuple(value.get("disabled_tools")),
        headers=_string_dict(value.get("headers")),
        source_path=source_path,
    )


def _string(value: object, *, default: str | None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a non-empty string")
    return value.strip()


def _bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("Expected a boolean")
    return value


def _positive_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError("Expected a positive number")
    return float(value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("Expected a list of strings")
    return tuple(str(item) for item in value)


def _optional_string_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _string_tuple(value)


def _string_dict(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Expected a string object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("Expected a string object")
        result[key] = item
    return result


def _path(value: object, *, source_path: Path, root: Path) -> Path | None:
    raw = _string(value, default=None)
    if raw is None:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = root if raw.startswith(".") else source_path.parent
    return (base / path).resolve()


def server_supports_tool(server: McpServerConfig, tool_name: str) -> bool:
    """Return whether config exposes a tool from a server."""
    if server.enabled_tools is not None and tool_name not in server.enabled_tools:
        return False
    return tool_name not in server.disabled_tools


def compact_tool_schema(schema: object, *, include_schema: bool) -> object | None:
    """Return a compact schema for optional inspection output."""
    if not include_schema or not isinstance(schema, dict):
        return None
    schema_dict = cast(dict[str, object], schema)
    allowed: dict[str, Any] = {}
    for key in ("type", "properties", "required", "additionalProperties"):
        if key in schema_dict:
            allowed[key] = schema_dict[key]
    return allowed
