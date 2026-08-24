"""Compact inspection metadata for downstream MCP servers and tools."""

from __future__ import annotations

from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import compact_tool_schema
from yoke.mcp.config import McpServerConfig


def matches_server(server: McpServerConfig, needle: str | None) -> bool:
    """Return whether a search term matches a server name."""
    if needle is None:
        return True
    return needle in server.name.lower()


def matches_tool(tool: McpToolInfo, needle: str | None) -> bool:
    """Return whether a search term matches tool metadata."""
    if needle is None:
        return True
    return needle in tool.name.lower() or needle in tool.description.lower()


def tool_summary(tool: McpToolInfo, *, include_schema: bool) -> dict[str, object]:
    """Return compact model-facing metadata for one downstream tool."""
    description = " ".join(tool.description.split())
    if len(description) > 240:
        description = description[:239].rstrip() + "…"
    summary: dict[str, object] = {"name": tool.name, "description": description}
    schema = compact_tool_schema(tool.input_schema, include_schema=include_schema)
    if schema is not None:
        summary["input_schema"] = schema
    return summary
