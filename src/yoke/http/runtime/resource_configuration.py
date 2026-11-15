"""Session-local tools, MCP policy, and client binding owned by resources."""

from __future__ import annotations

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.tools.context import ToolSessionContext
from yoke.http.runtime.client_tools import install_client_tools
from yoke.http.services.client_session import ClientSession
from yoke.mcp.config import McpSessionPolicy, McpSessionServerPolicy


class ResourceConfiguration:
    """Keep process-local settings when primary agents/providers are replaced."""

    def __init__(self) -> None:
        self.enabled_tools: set[str] | None = None
        self.session = ToolSessionContext(mcp_policy=McpSessionPolicy.empty())

    @property
    def client(self) -> ClientSession | None:
        return self.session.client_session

    @property
    def policy(self) -> McpSessionPolicy:
        assert self.session.mcp_policy is not None
        return self.session.mcp_policy

    def bind(self, agent: RuntimeAgent) -> None:
        agent.tool_session_context = self.session
        if self.client is not None:
            install_client_tools(agent)

    def set_mcp_policy(
        self,
        primary: RuntimeAgent | None,
        server_name: str,
        *,
        enabled: bool | None,
        enabled_tools: tuple[str, ...] | None,
        disabled_tools: tuple[str, ...] | None,
        update_enabled_tools: bool,
        update_disabled_tools: bool,
    ) -> None:
        existing = self.policy.servers.get(server_name)
        self.policy.servers[server_name] = McpSessionServerPolicy(
            enabled=enabled
            if enabled is not None
            else existing.enabled
            if existing
            else None,
            enabled_tools=enabled_tools
            if update_enabled_tools
            else existing.enabled_tools
            if existing
            else None,
            disabled_tools=disabled_tools
            if update_disabled_tools
            else existing.disabled_tools
            if existing
            else None,
        )
        if primary is not None:
            self.bind(primary)
            primary.configure_session_tools()

    def configure_client(
        self, primary: RuntimeAgent | None, client: ClientSession | None
    ) -> None:
        previous = self.client
        if (
            client is not None
            and previous is not None
            and previous.config.owner == client.config.owner
        ):
            client.latest_plan = previous.latest_plan
        self.session.client_session = client
        self.policy.injected_servers = (
            tuple(server.config(client.root) for server in client.config.mcp_servers)
            if client is not None
            else ()
        )
        if primary is not None:
            self.bind(primary)
            primary.refresh_tools(force=True)

    def set_tools(
        self,
        primary: RuntimeAgent | None,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        base = (
            set(self.enabled_tools)
            if self.enabled_tools is not None
            else set(default_enabled_names)
        )
        next_enabled = (base | enabled) - disabled
        self.enabled_tools = set(next_enabled)
        if primary is not None:
            hidden_names = set(primary.tools) - discovered_names
            primary.set_session_enabled_tools(next_enabled | hidden_names)
            primary.configure_session_tools(refresh=False, filter_instructions=True)
        return set(next_enabled)

    def enabled_snapshot(self) -> set[str] | None:
        return set(self.enabled_tools) if self.enabled_tools is not None else None

    def policy_snapshot(self) -> McpSessionPolicy:
        return McpSessionPolicy(
            servers=dict(self.policy.servers),
            injected_servers=self.policy.injected_servers,
        )
