"""Runtime tool and MCP operations used by terminal menus.

Persisted config edits remain client-side. Runtime inventory and temporary
selection are the same operations exposed by the HTTP tool and MCP routes.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from yoke.mcp.config import McpConfig
from yoke.mcp.manager import McpToolInfo

from yoke.agent.loop.agent import RuntimeAgent
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.mcp.config import (
    McpSessionPolicy,
    McpSessionServerPolicy,
    McpServerConfig,
    server_supports_tool,
)


class ToolInventory:
    """GET /tool and PATCH /session/{id}/tool."""

    def __init__(self, agent: object) -> None:
        self._agent = agent

    def report(self) -> ToolLoadReport | None:
        report = getattr(self._agent, "tool_report", None)
        return report if isinstance(report, ToolLoadReport) else None

    def active_names(self) -> set[str]:
        return (
            set(self._agent.tools) if isinstance(self._agent, RuntimeAgent) else set()
        )

    def select(
        self,
        active_names: set[str],
        visible_names: set[str],
        report: ToolLoadReport,
        *,
        ordered_names: Sequence[str],
    ) -> None:
        """PATCH /session/{id}/tool; retain hidden runtime tools."""
        agent = self._agent
        if not isinstance(agent, RuntimeAgent):
            return
        discovered = {entry.tool.name: entry.tool for entry in report.discovered_tools}
        selected = [discovered[name] for name in ordered_names if name in active_names]
        selected.extend(
            tool for name, tool in agent.tools.items() if name not in visible_names
        )
        agent.apply_tool_selection(
            selected, report=tool_report_with_active_names(report, active_names)
        )


def tool_report_with_active_names(
    report: ToolLoadReport, active_names: set[str]
) -> ToolLoadReport:
    """Project the tool inventory after a session selection."""
    return ToolLoadReport(
        discovered_tools=list(report.discovered_tools),
        active_tools=[
            entry
            for entry in report.discovered_tools
            if entry.tool.name in active_names
        ],
        denied_tools=[
            entry
            for entry in report.discovered_tools
            if entry.tool.name not in active_names
        ],
        config_path=report.config_path,
        unmatched_tool_names=report.unmatched_tool_names,
        unmatched_capability_ids=report.unmatched_capability_ids,
    )


class McpInventory:
    """GET/PATCH /session/{id}/mcp and GET /mcp."""

    def __init__(self, agent: object, root: Path) -> None:
        self._agent = agent
        self._root = root

    def _policy(self) -> McpSessionPolicy:
        agent = self._agent
        if not isinstance(agent, RuntimeAgent):
            return McpSessionPolicy.empty()
        existing = agent.tool_session_context.mcp_policy
        if isinstance(existing, McpSessionPolicy):
            return existing
        policy = McpSessionPolicy.empty()
        agent.tool_session_context.mcp_policy = policy
        return policy

    def config(self) -> McpConfig:
        """GET /session/{id}/mcp, using the session overlay."""
        from yoke.mcp.config import load_mcp_config

        return load_mcp_config(
            root=self._root, home=Path.home(), session_policy=self._policy()
        )

    def list_tools(self, server: McpServerConfig) -> tuple[McpToolInfo, ...]:
        """GET /session/{id}/mcp: tool inventory for a configured server."""
        from yoke.mcp.manager import McpManager

        manager = McpManager.from_paths(root=self._root, home=Path.home())
        try:
            return manager.list_configured_tools(server)
        finally:
            manager.close()

    def set_enabled(self, server: McpServerConfig, enabled: bool) -> None:
        """PATCH /session/{id}/mcp/{server.name} with enabled."""
        policy = self._policy()
        existing = policy.servers.get(server.name)
        policy.servers[server.name] = McpSessionServerPolicy(
            enabled=enabled,
            enabled_tools=existing.enabled_tools if existing else None,
            disabled_tools=existing.disabled_tools if existing else None,
        )

    def toggle_tool(self, server: McpServerConfig, tool_name: str) -> None:
        """PATCH /session/{id}/mcp/{server.name} with tool selection."""
        _toggle_session_mcp_tool(
            session_policy=self._policy(), server=server, tool_name=tool_name
        )

    def refresh(self) -> None:
        """PATCH /session/{id}/mcp: refresh while keeping non-MCP selection."""
        agent = self._agent
        if not isinstance(agent, RuntimeAgent):
            return
        old_non_mcp_names = {
            name for name in agent.tools if name not in {"mcp_inspect", "mcp_call"}
        }
        agent.refresh_tools(force=True)
        agent.tools = {
            name: tool
            for name, tool in agent.tools.items()
            if name in {"mcp_inspect", "mcp_call"} or name in old_non_mcp_names
        }


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


def _remove_value(values: list[str], value: str) -> None:
    values[:] = [item for item in values if item != value]
