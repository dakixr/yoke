"""Lazy public exports for Yoke's low-context MCP integration."""

from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.mcp.config import McpConfig as McpConfig
    from yoke.mcp.config import McpServerConfig as McpServerConfig
    from yoke.mcp.config import McpSessionPolicy as McpSessionPolicy
    from yoke.mcp.config import McpSessionServerPolicy as McpSessionServerPolicy
    from yoke.mcp.config import load_mcp_config as load_mcp_config
    from yoke.mcp.editing import (
        patch_persisted_mcp_server as patch_persisted_mcp_server,
    )
    from yoke.mcp.editing import persisted_mcp_scope_path as persisted_mcp_scope_path
    from yoke.mcp.editing import (
        set_persisted_mcp_server_enabled as set_persisted_mcp_server_enabled,
    )
    from yoke.mcp.editing import toggle_persisted_mcp_tool as toggle_persisted_mcp_tool
    from yoke.mcp.manager import McpManager as McpManager

_LAZY_EXPORTS = {
    "McpConfig": ("yoke.mcp.config", "McpConfig"),
    "McpManager": ("yoke.mcp.manager", "McpManager"),
    "McpServerConfig": ("yoke.mcp.config", "McpServerConfig"),
    "McpSessionPolicy": ("yoke.mcp.config", "McpSessionPolicy"),
    "McpSessionServerPolicy": ("yoke.mcp.config", "McpSessionServerPolicy"),
    "load_mcp_config": ("yoke.mcp.config", "load_mcp_config"),
    "patch_persisted_mcp_server": (
        "yoke.mcp.editing",
        "patch_persisted_mcp_server",
    ),
    "persisted_mcp_scope_path": ("yoke.mcp.editing", "persisted_mcp_scope_path"),
    "set_persisted_mcp_server_enabled": (
        "yoke.mcp.editing",
        "set_persisted_mcp_server_enabled",
    ),
    "toggle_persisted_mcp_tool": (
        "yoke.mcp.editing",
        "toggle_persisted_mcp_tool",
    ),
}

__all__ = [
    "McpConfig",
    "McpManager",
    "McpServerConfig",
    "McpSessionPolicy",
    "McpSessionServerPolicy",
    "load_mcp_config",
    "patch_persisted_mcp_server",
    "persisted_mcp_scope_path",
    "set_persisted_mcp_server_enabled",
    "toggle_persisted_mcp_tool",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve MCP exports without importing transports until they are needed."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
