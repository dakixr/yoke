"""Model-facing low-context MCP tools."""

# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any
from typing import Protocol
from typing import TYPE_CHECKING
from typing import cast

from pydantic import Field

from yoke.agent.tools.base import LocalTool

if TYPE_CHECKING:
    from collections.abc import Callable

    from yoke.mcp.config import McpSessionPolicy


class McpToolManager(Protocol):
    """Minimal manager contract used by the model-facing MCP tools."""

    def has_servers(self) -> bool: ...

    def inspect(
        self,
        *,
        server: str | None = None,
        query: str | None = None,
        include_schemas: bool = False,
    ) -> dict[str, object]: ...

    def call_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        cancel_requested: Callable[[], bool],
    ) -> dict[str, object]: ...

    def close(self) -> None: ...


class LazyMcpManager:
    """Delay MCP client and HTTP transport imports until an MCP tool executes."""

    def __init__(
        self,
        *,
        root: Path,
        home: Path,
        session_policy: McpSessionPolicy | None = None,
    ) -> None:
        self.root = root.resolve()
        self.home = home.resolve()
        self.session_policy = session_policy
        self._manager_lock = Lock()
        self._inner: McpToolManager | None = None

    def has_servers(self) -> bool:
        """Check enabled-server presence using only the lightweight config module."""
        from yoke.mcp.config import load_mcp_config

        config = load_mcp_config(
            root=self.root,
            home=self.home,
            session_policy=self.session_policy,
        )
        return bool(config.enabled_servers)

    def inspect(
        self,
        *,
        server: str | None = None,
        query: str | None = None,
        include_schemas: bool = False,
    ) -> dict[str, object]:
        return self._manager().inspect(
            server=server,
            query=query,
            include_schemas=include_schemas,
        )

    def call_tool(
        self,
        *,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        cancel_requested: Callable[[], bool],
    ) -> dict[str, object]:
        return self._manager().call_tool(
            server=server,
            tool=tool,
            arguments=arguments,
            cancel_requested=cancel_requested,
        )

    def close(self) -> None:
        """Close the real manager only when one was created."""
        with self._manager_lock:
            manager = self._inner
            self._inner = None
        if manager is not None:
            manager.close()

    def _manager(self) -> McpToolManager:
        manager = self._inner
        if manager is not None:
            return manager
        with self._manager_lock:
            manager = self._inner
            if manager is None:
                from yoke.mcp.manager import McpManager

                manager = McpManager.from_paths(
                    root=self.root,
                    home=self.home,
                    session_policy=self.session_policy,
                )
                self._inner = manager
            return manager


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

    def _manager(self) -> McpToolManager:
        manager = self._context.get("mcp_manager")
        if manager is None:
            raise RuntimeError("MCP manager is not configured")
        return cast(McpToolManager, manager)

    def owned_resources(self) -> tuple[object, ...]:
        """Return the shared MCP manager owned by this tool registration."""
        return (self._manager(),)


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
            cancel_requested=self._is_cancel_requested,
        )

    def _manager(self) -> McpToolManager:
        manager = self._context.get("mcp_manager")
        if manager is None:
            raise RuntimeError("MCP manager is not configured")
        return cast(McpToolManager, manager)

    def owned_resources(self) -> tuple[object, ...]:
        """Return the shared MCP manager owned by this tool registration."""
        return (self._manager(),)


def register_mcp_tools(manager: McpToolManager) -> tuple[LocalTool, ...]:
    """Return the compact MCP tools when at least one server is configured."""
    if not manager.has_servers():
        return ()
    return (
        McpInspectTool.bind(mcp_manager=manager),
        McpCallTool.bind(mcp_manager=manager),
    )
