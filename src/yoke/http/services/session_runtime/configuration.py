"""Install process-local client configuration under the native runtime gate."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.loop.agent import RuntimeAgent
from yoke.http.services.session_runtime.workspace import (
    mutates_workspace,
    synchronize_runtime_workspace,
)
from yoke.http.services.session_runtime.client_tools import install_client_tools
from yoke.http.errors import ApiError
from yoke.http.models.client_session import ClientSessionRequest
from yoke.http.services.client_session import ClientSession
from yoke.http.services.human_input_service import HumanInputService
from yoke.mcp.config import McpSessionServerPolicy

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime
    from yoke.http.services.session_runtime.resources import SessionRuntimeResources


def bind_client(resources: SessionRuntimeResources, agent: RuntimeAgent) -> None:
    object.__setattr__(
        agent.provider, "_yoke_mcp_session_policy", resources._mcp_session_policy
    )
    object.__setattr__(agent.provider, "_yoke_client_session", resources.client_session)
    if resources.client_session is not None:
        install_client_tools(agent)


def set_mcp_policy(
    resources: SessionRuntimeResources,
    server_name: str,
    *,
    enabled: bool | None,
    enabled_tools: tuple[str, ...] | None,
    disabled_tools: tuple[str, ...] | None,
    update_enabled_tools: bool,
    update_disabled_tools: bool,
) -> None:
    with resources.lock:
        existing = resources._mcp_session_policy.servers.get(server_name)
        resources._mcp_session_policy.servers[server_name] = McpSessionServerPolicy(
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
        primary = resources._primary_agent
        if primary is not None:
            bind_client(resources, primary)
            primary.refresh_tools(force=True)
            if primary._context is not None:
                primary._sync_context_instructions(primary._context)


async def configure_client(
    runtime: SessionRuntime,
    config: ClientSessionRequest,
    human_input: HumanInputService,
) -> dict[str, object]:
    def configure() -> dict[str, object]:
        if runtime.workers.active_count:
            raise ApiError(
                409,
                "session_busy",
                "Drain native workers before replacing client configuration.",
            )
        record = runtime.store.summary_record(runtime.session_id)
        if record is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        synchronize_runtime_workspace(runtime)
        root = Path(record.root or Path.cwd())
        resources = runtime.resources
        with resources.lock:
            previous = resources.client_session
            client = ClientSession(
                runtime.session_id, config, human_input, runtime.events, root
            )
            if previous is not None and previous.config.owner == config.owner:
                client.latest_plan = previous.latest_plan
            resources.client_session = client
            resources._mcp_session_policy.injected_servers = tuple(
                server.config(root) for server in config.mcp_servers
            )
            if resources._primary_agent is not None:
                bind_client(resources, resources._primary_agent)
                resources._primary_agent.refresh_tools(force=True)
        return {
            "mode": config.mode,
            "additionalDirectories": config.additional_directories,
            "mcpServers": [server.name for server in config.mcp_servers],
        }

    return await runtime.idle_mutation(configure)


async def detach_client(runtime: SessionRuntime, owner: str) -> None:
    def detach() -> None:
        if runtime.workers.active_count:
            raise ApiError(
                409,
                "session_busy",
                "Drain native workers before detaching client configuration.",
            )
        resources = runtime.resources
        with resources.lock:
            client = resources.client_session
            if client is None or client.config.owner != owner:
                return
            resources.client_session = None
            resources._mcp_session_policy.injected_servers = ()
            if resources._primary_agent is not None:
                bind_client(resources, resources._primary_agent)
                resources._primary_agent.refresh_tools(force=True)

    await runtime.idle_mutation(detach)


@mutates_workspace
async def apply_mcp_policy(
    self: SessionRuntime,
    server_name: str,
    *,
    enabled: bool | None,
    enabled_tools: tuple[str, ...] | None,
    disabled_tools: tuple[str, ...] | None,
    update_enabled_tools: bool = False,
    update_disabled_tools: bool = False,
) -> None:
    """Apply session-only MCP server and tool policy."""
    async with self._lock:
        self._require_no_work_locked()
        self.resources.set_mcp_policy(
            server_name,
            enabled=enabled,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
            update_enabled_tools=update_enabled_tools,
            update_disabled_tools=update_disabled_tools,
        )
        self.events.live(
            "session.mcp.updated",
            {"server": server_name},
            session_id=self.session_id,
            location=self._event_location,
        )
