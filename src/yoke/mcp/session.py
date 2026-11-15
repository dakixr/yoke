"""Transport-neutral MCP session lifecycle and tool catalogue handling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from yoke.mcp.config import McpServerConfig

JSON = dict[str, Any]
MCP_PROTOCOL_VERSION = "2025-03-26"


class McpClientError(RuntimeError):
    """Raised when an MCP client operation fails."""


@dataclass(slots=True, frozen=True)
class McpToolInfo:
    """Compact MCP tool metadata."""

    name: str
    description: str
    input_schema: JSON
    output_schema: JSON | None = None
    annotations: JSON | None = None


class McpSession:
    """Shared MCP handshake, catalogue, and tool-call behavior."""

    def __init__(self, server: McpServerConfig, *, root: Path) -> None:
        self.server = server
        self.root = root.resolve()
        self._initialized = False
        self._tool_cache: tuple[McpToolInfo, ...] | None = None
        self._tools_changed = False
        self.server_instructions: str | None = None

    def _open_transport(self) -> None:
        """Open the concrete transport before the common handshake."""

    def _close_transport(self) -> None:
        """Close the concrete transport after the common session teardown."""

    def _after_initialize(self, result: JSON) -> None:
        """Allow a transport to capture handshake metadata."""

    def request(
        self,
        method: str,
        params: JSON | None = None,
        *,
        timeout: float,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> JSON:
        raise NotImplementedError

    def notify(self, method: str, params: JSON | None = None) -> None:
        raise NotImplementedError

    def start(self) -> None:
        """Open and initialize the MCP session exactly once."""
        if self._initialized:
            return
        self._open_transport()
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {"name": "yoke", "version": "0"},
                },
                timeout=self.server.startup_timeout_sec,
            )
            self._after_initialize(result)
            instructions = result.get("instructions")
            if isinstance(instructions, str) and instructions.strip():
                self.server_instructions = instructions.strip()
            self.notify("notifications/initialized")
            self._initialized = True
        except BaseException:
            self.close()
            raise

    def list_tools(self, *, force: bool = False) -> tuple[McpToolInfo, ...]:
        """List and parse every page of the server's tool catalogue."""
        self.start()
        if self._tool_cache is not None and not force and not self._tools_changed:
            return self._tool_cache
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        for _page in range(100):
            result = self.request(
                "tools/list",
                {"cursor": cursor} if cursor else None,
                timeout=self.server.tool_timeout_sec,
            )
            raw_tools = result.get("tools", [])
            if not isinstance(raw_tools, list):
                raise McpClientError("MCP tools/list returned invalid tools")
            tools.extend(_parse_tools(raw_tools))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        else:
            raise McpClientError("MCP tools/list exceeded 100 pages")
        self._tool_cache = tuple(tools)
        self._tools_changed = False
        return self._tool_cache

    def call_tool(
        self,
        name: str,
        arguments: JSON,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> JSON:
        """Call one MCP tool through the concrete request transport."""
        self.start()
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.server.tool_timeout_sec,
            cancel_requested=cancel_requested,
        )

    def close(self) -> None:
        """Close the transport and reset session state."""
        self._close_transport()
        self._initialized = False


def _parse_tools(raw_tools: list[object]) -> list[McpToolInfo]:
    """Parse a tools/list page while ignoring malformed entries."""
    tools: list[McpToolInfo] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        description = item.get("description")
        schema = item.get("inputSchema") or item.get("input_schema") or {}
        input_schema = cast(JSON, schema) if isinstance(schema, dict) else {}
        output_schema = item.get("outputSchema")
        annotations = item.get("annotations")
        tools.append(
            McpToolInfo(
                name=name,
                description=description if isinstance(description, str) else "",
                input_schema=input_schema,
                output_schema=cast(JSON, output_schema)
                if isinstance(output_schema, dict)
                else None,
                annotations=cast(JSON, annotations)
                if isinstance(annotations, dict)
                else None,
            )
        )
    return tools
