"""Tool-only MCP server backed by Yoke's local execution tools."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yoke.mcp_server.config import MCPServerConfig
    from yoke.mcp_server.server import MCPService
    from yoke.mcp_server.server import create_service

_EXPORTS = {
    "MCPServerConfig": "yoke.mcp_server.config",
    "MCPService": "yoke.mcp_server.server",
    "create_service": "yoke.mcp_server.server",
}


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Keep the injected subprocess bridge independent of server startup."""
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


__all__ = ["MCPServerConfig", "MCPService", "create_service"]
