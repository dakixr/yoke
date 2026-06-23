"""Model-facing low-context MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from yoke.agent.tools.base import LocalTool
from yoke.mcp.manager import McpManager


class McpInspectTool(LocalTool):
    """Inspect configured MCP servers without exposing the full catalog eagerly."""

    name = "mcp_inspect"
    description = (
        "Inspect configured MCP servers and compact tool metadata. Use this before "
        "mcp_call when you need to discover available MCP-backed capabilities."
    )
    execute_in_process = True

    server: str | None = Field(
        default=None,
        description="Optional MCP server name to inspect.",
    )
    query: str | None = Field(
        default=None,
        description="Optional case-insensitive filter for tool names/descriptions.",
    )
    include_schemas: bool = Field(
        default=False,
        description="Include compact input schemas for matching tools.",
    )

    def execute(self) -> dict[str, object]:
        """Return compact MCP server/tool metadata."""
        return self._manager().inspect(
            server=self.server,
            query=self.query,
            include_schemas=self.include_schemas,
        )

    def _manager(self) -> McpManager:
        manager = self._context.get("mcp_manager")
        if not isinstance(manager, McpManager):
            raise RuntimeError("MCP manager is not configured")
        return manager


class McpCallTool(LocalTool):
    """Call one configured MCP server tool."""

    name = "mcp_call"
    description = (
        "Call a tool exposed by a configured MCP server. Prefer mcp_inspect first "
        "to discover server and tool names. Pass only the selected tool arguments."
    )
    execute_in_process = True

    server: str = Field(description="Configured MCP server name.", min_length=1)
    tool: str = Field(description="MCP tool name on that server.", min_length=1)
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON object of arguments to pass to the MCP tool.",
    )

    def execute(self) -> dict[str, object]:
        """Call the selected MCP tool."""
        return self._manager().call_tool(
            server=self.server,
            tool=self.tool,
            arguments=self.arguments,
        )

    def _manager(self) -> McpManager:
        manager = self._context.get("mcp_manager")
        if not isinstance(manager, McpManager):
            raise RuntimeError("MCP manager is not configured")
        return manager


def register_mcp_tools(manager: McpManager) -> tuple[LocalTool, ...]:
    """Return the compact MCP tools when at least one server is configured."""
    if not manager.has_servers():
        return ()
    return (
        McpInspectTool.bind(mcp_manager=manager),
        McpCallTool.bind(mcp_manager=manager),
    )
