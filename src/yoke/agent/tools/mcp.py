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
        "Inspect one configured MCP server and its tool metadata. Use this before "
        "mcp_call when you need schemas or available tools for a known server."
    )
    execute_in_process = True

    server: str = Field(
        description="Exact configured MCP server name to inspect.",
        min_length=1,
    )
    query: str | None = Field(
        default=None,
        description="Optional case-insensitive filter for tool names/descriptions.",
    )
    include_schemas: bool = Field(
        default=True,
        description="Include full input schemas for matching tools.",
    )

    def execute(self) -> dict[str, object]:
        """Return compact MCP server/tool metadata."""
        return self._manager().inspect(
            server=self.server,
            query=self.query,
            include_schemas=self.include_schemas,
        )

    def parse_arguments(self, arguments: dict[str, object]) -> LocalTool:
        """Parse and enforce the current configured server allow-list."""
        parsed = super().parse_arguments(arguments)
        manager = self._manager()
        server_names = {server.name for server in manager.servers}
        parsed_server = parsed.server if isinstance(parsed, McpInspectTool) else None
        if parsed_server not in server_names:
            allowed = ", ".join(sorted(server_names)) or "none"
            raise ValueError(
                f"Unknown MCP server: {parsed_server}. Expected one of: {allowed}"
            )
        return parsed

    def to_definition(self) -> dict[str, object]:
        """Return a tool definition that advertises current MCP servers."""
        manager = self._manager()
        server_names = [server.name for server in manager.servers]
        schema = self.__class__.model_json_schema(by_alias=True)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            server_property = properties.get("server")
            if isinstance(server_property, dict):
                server_property["enum"] = server_names
                server_property["description"] = _server_argument_description(manager)
        schema["required"] = ["server"]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": _inspect_tool_description(manager),
                "parameters": schema,
            },
        }

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


def _inspect_tool_description(manager: McpManager) -> str:
    server_lines = _server_lines(manager)
    if not server_lines:
        return McpInspectTool.description
    return (
        f"{McpInspectTool.description} Configured MCP servers: "
        + "; ".join(server_lines)
        + "."
    )


def _server_argument_description(manager: McpManager) -> str:
    server_lines = _server_lines(manager)
    if not server_lines:
        return "Exact configured MCP server name to inspect."
    return (
        "Exact configured MCP server name to inspect. Available servers: "
        + "; ".join(server_lines)
        + "."
    )


def _server_lines(manager: McpManager) -> list[str]:
    return [
        (
            f"{server.name} ({server.description})"
            if server.description
            else f"{server.name} ({server.transport})"
        )
        for server in manager.servers
    ]
