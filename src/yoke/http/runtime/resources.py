"""Owned agents, providers, process subscriptions, and session-local policy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Executor, Future
from threading import Lock
from typing import TYPE_CHECKING

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.protocols import AgentRunner
from yoke.http.runtime.agent_runs import SessionAgentRuns
from yoke.agent.provider_selection import capture_provider_session_state
from yoke.http.services.privacy import SessionPrivacy
from yoke.environment import (
    ExecutionEnvironment,
    current_environment,
    execution_environment,
    project_environment,
)
from yoke.agent.models import ConversationEntry
from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.context import ToolSessionContext
from yoke.http.runtime.client_tools import install_client_tools
from yoke.http.runtime.cleanup import AgentCleanup as _AgentCleanup
from yoke.http.runtime.cleanup import RetiredProvider
from yoke.http.runtime.owned_cleanup import retire_owned_resource
from yoke.http.runtime.processes import ResourceProcesses
from yoke.http.runtime.resource_configuration import (
    ResourceConfiguration,
)
from yoke.http.services.client_session import ClientSession
from yoke.mcp.config import McpSessionPolicy
from yoke.session import SessionRecord, SessionStore
from yoke.session.workspace import promote_session_turn as promote_runtime_fork
from yoke.session.workspace import require_session_workspace

if TYPE_CHECKING:
    from yoke.http.runtime.factory import SessionAgentFactory

LOGGER = logging.getLogger(__name__)


class SessionRuntimeResources:
    """Own mutable runtime resources separately from logical turn state."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_factory: SessionAgentFactory,
        executor: Executor,
        on_process_change: Callable[[], None],
        store: SessionStore | None = None,
        privacy: SessionPrivacy | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent_factory = agent_factory
        self.executor = executor
        self.on_process_change = on_process_change
        self.store = store
        self.privacy = privacy or SessionPrivacy()
        self._primary_root: str | None = None
        self.agent_runs = SessionAgentRuns(self.process_managers)
        self.processes = ResourceProcesses(store, session_id, on_process_change)
        self._client_primaries: dict[int, RuntimeAgent] = {}
        self._client_processes: dict[int, ResourceProcesses] = {}
        self.lock = Lock()
        self._primary_agent: RuntimeAgent | None = None
        self.configuration = ResourceConfiguration()
        self._turn_agents: dict[int, RuntimeAgent] = {}
        self._providers: dict[int, object] = {}
        self._claimed_providers: set[int] = set()
        self._cleanups: dict[int, _AgentCleanup] = {}
        self._close_completion: Future[None] | None = None
        self._closing = False
        self._closed = False

    def environment(self, record: SessionRecord) -> ExecutionEnvironment:
        """Resolve a scope without exposing the values to persistence."""
        import os
        from pathlib import Path

        selected = self.client_session or self.configuration.clients.fallback
        return current_environment() or (
            selected.environment
            if selected is not None
            else project_environment(Path(record.root or Path.cwd()), os.environ)
        )

    def build_agent(self, record: SessionRecord) -> AgentRunner:
        """Construct with a client environment or a copied project fallback."""
        environment = self.environment(record)
        self.privacy.register(environment.values)
        with execution_environment(environment), self.privacy.scope():
            agent = self.agent_factory(record)
        return agent

    def primary_locked(self) -> RuntimeAgent | None:
        """Return the primary agent while the caller holds ``lock``."""
        return self._primary_agent

    def has_primary(self) -> bool:
        with self.lock:
            return self._primary_agent is not None or bool(self._client_primaries)

    def ensure_primary(
        self,
        record: SessionRecord,
        *,
        load_state: bool,
        active_entries: list[ConversationEntry] | None = None,
        load_active_entries: Callable[[], list[ConversationEntry]] | None = None,
    ) -> RuntimeAgent:
        """Build the owned primary and optionally load its active path."""
        if self.store is not None:
            require_session_workspace(record)
        self.synchronize_workspace(record)
        with self.lock:
            if self._closing:
                raise RuntimeError("HTTP runtime resources are closing.")
            if self._primary_agent is None:
                candidate = self.build_agent(record)
                if not isinstance(candidate, RuntimeAgent):
                    raise TypeError(
                        "HTTP session factories must return a RuntimeAgent."
                    )
                self._primary_agent = candidate
                self._primary_root = record.root
                self._providers[id(candidate.provider)] = candidate.provider
                self.configuration.bind(candidate)
                candidate.refresh_tools(force=True)
                self.processes.attach(candidate.command_process_manager)
                if self.configuration.enabled_tools is not None:
                    candidate.set_session_enabled_tools(
                        self.configuration.enabled_tools
                    )
            primary = self._primary_agent
            if load_state:
                if active_entries is None:
                    if load_active_entries is None:
                        raise ValueError(
                            "Active entries are required when loading state."
                        )
                    active_entries = load_active_entries()
                primary.load_owned_conversation(
                    active_entries,
                    available_skills=primary.available_skills,
                    active_skills=record.active_skills,
                )
            return primary

    def has_live_work(self) -> bool:
        """Include retired turns and background processes, not only UI activity."""
        with self.lock:
            primary = self._primary_agent
            agents = [*self._client_primaries.values()]
            if primary is not None:
                agents.append(primary)
            return bool(self._turn_agents or self._cleanups) or any(
                item.status == "running"
                for agent in agents
                for item in agent.command_process_manager.snapshots()
            )

    def register_provider(self, provider: object) -> None:
        """Own a replacement provider even if later metadata persistence fails."""
        with self.lock:
            self._providers[id(provider)] = provider

    def retire_provider(self, provider: object) -> None:
        """Close replaced providers only after their remaining agents retire."""
        with self.lock:
            self._providers[id(provider)] = provider
            self._submit_cleanup_locked(RetiredProvider(provider))

    def prepare_turn(
        self,
        record: SessionRecord,
        *,
        active_entries: list[ConversationEntry] | None,
        load_active_entries: Callable[[], list[ConversationEntry]],
        client: ClientSession | None = None,
    ) -> RuntimeAgent:
        """Create an isolated turn fork with the established runtime projection."""
        if client is not None:
            with self.lock:
                if self._closing:
                    raise RuntimeError("HTTP runtime resources are closing.")
                primary = self._client_primaries.get(id(client))
                if primary is not None and record.model_id is not None:
                    state = capture_provider_session_state(primary)
                    if (
                        state.provider_name,
                        state.model_id,
                        state.reasoning_effort,
                    ) != (
                        record.provider_name,
                        record.model_id,
                        record.reasoning_effort,
                    ):
                        self._client_primaries.pop(id(client))
                        self._submit_cleanup_locked(primary)
                        primary = None
                if primary is None:
                    with execution_environment(client.environment):
                        candidate = self.build_agent(record)
                    if not isinstance(candidate, RuntimeAgent):
                        raise TypeError(
                            "HTTP session factories must return a RuntimeAgent."
                        )
                    primary = candidate
                    self._client_primaries[id(client)] = primary
                    self._providers[id(primary.provider)] = primary.provider
                    try:
                        session = ToolSessionContext(
                            client_session=client,
                            environment=dict(client.environment.values),
                            cwd=client.environment.cwd,
                            mcp_policy=self.configuration.policy_snapshot(),
                        )
                        assert session.mcp_policy is not None
                        session.mcp_policy.injected_servers = tuple(
                            server.config(client.root)
                            for server in client.config.mcp_servers
                        )
                        primary.tool_session_context = session
                        install_client_tools(primary)
                        primary.refresh_tools(force=True)
                        processes = ResourceProcesses(
                            self.store, self.session_id, self.on_process_change
                        )
                        processes.attach(primary.command_process_manager)
                        self._client_processes[id(primary)] = processes
                    except BaseException:
                        self._submit_cleanup_locked(primary)
                        raise
                primary.tool_session_context.mcp_policy = (
                    self.configuration.policy_snapshot()
                )
                assert primary.tool_session_context.mcp_policy is not None
                primary.tool_session_context.mcp_policy.injected_servers = tuple(
                    server.config(client.root) for server in client.config.mcp_servers
                )
                primary.active_skills = [
                    skill.model_copy(deep=True) for skill in record.active_skills
                ]
                primary.refresh_tools(force=True)
                if self.configuration.enabled_tools is not None:
                    primary.set_session_enabled_tools(self.configuration.enabled_tools)
                with execution_environment(client.environment):
                    turn_agent = primary.fork(
                        isolate_provider=True, include_state=False
                    )
                self._turn_agents[id(turn_agent)] = turn_agent
                self._providers[id(turn_agent.provider)] = turn_agent.provider
                try:
                    turn_agent.load_owned_conversation(
                        active_entries
                        if active_entries is not None
                        else load_active_entries(),
                        available_skills=primary.available_skills,
                        active_skills=record.active_skills,
                    )
                    return turn_agent
                except BaseException:
                    self._submit_cleanup_locked(turn_agent)
                    raise

        self.ensure_primary(record, load_state=False)
        with self.lock:
            primary = self._primary_agent
            if primary is None or self._closing:
                raise RuntimeError("HTTP runtime resources are closing.")
            turn_agent = primary.fork(isolate_provider=True, include_state=False)
            self._turn_agents[id(turn_agent)] = turn_agent
            self._providers[id(turn_agent.provider)] = turn_agent.provider
            try:
                if active_entries is None:
                    active_entries = load_active_entries()
                turn_agent.load_owned_conversation(
                    active_entries,
                    available_skills=primary.available_skills,
                    active_skills=primary.active_skills,
                )
            except Exception:
                self._submit_cleanup_locked(turn_agent)
                raise
            return turn_agent

    def promote(self, turn_agent: object | None) -> None:
        """Promote a completed fork while retaining the primary identity."""
        if not isinstance(turn_agent, RuntimeAgent):
            return
        with self.lock:
            client = turn_agent.tool_session_context.client_session
            primary = (
                self._client_primaries.get(id(client))
                if client is not None
                else self._primary_agent
            )
            if primary is not None and turn_agent is not primary:
                promote_runtime_fork(primary, turn_agent)

    def process_managers(self) -> list[CommandProcessManager]:
        """Include client-owned processes retained beyond their physical turns."""
        with self.lock:
            managers = [
                agent.command_process_manager
                for agent in self._client_primaries.values()
            ]
            if self._primary_agent is not None:
                managers.append(self._primary_agent.command_process_manager)
            return managers

    def process_manager(self) -> CommandProcessManager | None:
        """Return the primary agent's process manager when it exists."""
        with self.lock:
            primary = self._primary_agent
            if primary is None:
                primary = next(reversed(self._client_primaries.values()), None)
            return primary.command_process_manager if primary is not None else None

    def session_enabled_tool_names(self) -> set[str] | None:
        with self.lock:
            return self.configuration.enabled_snapshot()

    def mcp_session_policy(self) -> McpSessionPolicy:
        with self.lock:
            return self.configuration.policy_snapshot()

    def set_mcp_policy(
        self,
        server_name: str,
        *,
        enabled: bool | None,
        enabled_tools: tuple[str, ...] | None,
        disabled_tools: tuple[str, ...] | None,
        update_enabled_tools: bool,
        update_disabled_tools: bool,
    ) -> None:
        """Update MCP policy and refresh the primary agent if loaded."""
        with self.lock:
            self.configuration.set_mcp_policy(
                self._primary_agent,
                server_name,
                enabled=enabled,
                enabled_tools=enabled_tools,
                disabled_tools=disabled_tools,
                update_enabled_tools=update_enabled_tools,
                update_disabled_tools=update_disabled_tools,
            )

    def set_tools(
        self,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        """Update the tool allowlist and refresh the primary agent if loaded."""
        with self.lock:
            return self.configuration.set_tools(
                self._primary_agent,
                discovered_names=discovered_names,
                default_enabled_names=default_enabled_names,
                enabled=enabled,
                disabled=disabled,
            )

    async def reap(self, turn_agent: object | None) -> None:
        """Join the single daemon cleanup for one completed turn agent."""
        completion = self.retire(turn_agent)
        if completion is not None:
            await asyncio.shield(asyncio.wrap_future(completion))

    def retire(self, turn_agent: object | None) -> Future[None] | None:
        """Transfer a completed turn agent to the daemon cleanup."""
        if turn_agent is None:
            return None
        with self.lock:
            cleanup = self._cleanups.get(id(turn_agent))
            if cleanup is None:
                if self._turn_agents.get(id(turn_agent)) is not turn_agent:
                    return None
                cleanup = self._submit_cleanup_locked(turn_agent)
            return cleanup.completion

    async def close(self) -> None:
        """Join one close operation without tying it to the runtime executor."""
        with self.lock:
            if self._closed:
                return
            first = not self._closing
            if first:
                self._closing = True
                self._close_completion = Future()
                unsubscribe = self.processes.unsubscribe
            else:
                unsubscribe = None
            completion = self._close_completion
            assert completion is not None
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to unsubscribe an HTTP process listener.")
        if first:
            with self.lock:
                if self._primary_agent is not None:
                    self._submit_cleanup_locked(self._primary_agent)
                for primary in list(self._client_primaries.values()):
                    self._submit_cleanup_locked(primary)
                self._maybe_finish_close_locked()
        await asyncio.shield(asyncio.wrap_future(completion))

    def _submit_cleanup_locked(self, agent: object) -> _AgentCleanup:
        cleanup = self._cleanups.get(id(agent))
        if cleanup is not None:
            return cleanup
        cleanup = _AgentCleanup(self, agent)
        self._cleanups[id(agent)] = cleanup
        cleanup.completion = retire_owned_resource(cleanup.attempt)
        return cleanup

    def remove_terminal_agent(self, agent: object) -> object | None:
        provider = getattr(agent, "provider", None)
        with self.lock:
            if self._primary_agent is agent:
                self._primary_agent = None
            for identity, primary in list(self._client_primaries.items()):
                if primary is agent:
                    self._client_primaries.pop(identity)
            processes = self._client_processes.pop(id(agent), None)
            if processes is not None:
                processes.close()
            if self._turn_agents.get(id(agent)) is agent:
                self._turn_agents.pop(id(agent))
            if provider is None or self._provider_is_referenced_locked(provider):
                return None
            provider_id = id(provider)
            if provider_id in self._claimed_providers:
                return None
            self._claimed_providers.add(provider_id)
            return self._providers.get(provider_id)

    def _provider_is_referenced_locked(self, provider: object) -> bool:
        agents: list[object] = [
            *self._turn_agents.values(),
            *self._client_primaries.values(),
        ]
        if self._primary_agent is not None:
            agents.append(self._primary_agent)
        return any(getattr(agent, "provider", None) is provider for agent in agents)

    def remove_terminal_provider(self, provider: object) -> None:
        with self.lock:
            self._providers.pop(id(provider), None)
            self._claimed_providers.discard(id(provider))

    def finish_cleanup(self, cleanup: _AgentCleanup) -> None:
        with self.lock:
            if self._cleanups.get(id(cleanup.agent)) is cleanup:
                self._cleanups.pop(id(cleanup.agent))
            self._maybe_finish_close_locked()

    def _maybe_finish_close_locked(self) -> None:
        completion = self._close_completion
        if (
            not self._closing
            or self._primary_agent is not None
            or self._turn_agents
            or self._client_primaries
            or self._providers
            or self._cleanups
            or completion is None
        ):
            return
        self._closed = True
        self.agent_runs.close()
        self.processes.close()
        if not completion.done():
            completion.set_result(None)

    def synchronize_workspace(self, record: SessionRecord | None = None) -> None:
        """Retire stale configuration, including policy set before agent startup."""
        if record is None:
            if self.store is None:
                return
            record = self.store.summary_record(self.session_id)
            if record is None:
                return
        with self.lock:
            if self._primary_root is None:
                self._primary_root = record.root
            if self._primary_root == record.root:
                return
            old = self._primary_agent
            self.processes.close()
            self._primary_agent = None
            self._primary_root = record.root
            self.configuration = ResourceConfiguration()
            if old is not None:
                self._submit_cleanup_locked(old)

    @property
    def client_session(self) -> ClientSession | None:
        return self.configuration.client

    def configure_client(self, client: ClientSession | None) -> None:
        with self.lock:
            self.configuration.configure_client(None, client)

    async def reset_workspace(self, record: SessionRecord) -> None:
        """Reuse this resource owner after committed relocation and cleanup."""
        try:
            await self.close()
        finally:
            with self.lock:
                self._closing = self._closed = False
                self._close_completion = None
                self.configuration = ResourceConfiguration()
                self._primary_root = record.root
