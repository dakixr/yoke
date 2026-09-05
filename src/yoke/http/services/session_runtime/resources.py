"""Owned agents, providers, process subscriptions, and session-local policy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import Executor
import logging
from threading import Lock

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop.forking import promote_runtime_fork
from yoke.agent.models import ConversationEntry
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.mcp.config import McpSessionPolicy
from yoke.mcp.config import McpSessionServerPolicy
from yoke.http.services.session_runtime.reaper import retire_resource
from yoke.session import SessionRecord


LOGGER = logging.getLogger(__name__)
type SessionAgentFactory = Callable[[SessionRecord], object]


class _AgentCleanup:
    """Mutable state for one single-flight agent and provider cleanup."""

    def __init__(self, owner: SessionRuntimeResources, agent: object) -> None:
        self.owner = owner
        self.agent = agent
        self.agent_terminal = False
        self.provider: object | None = None
        self.failures = 0
        self.completion: Future[None] | None = None

    def attempt(self) -> bool:
        """Attempt agent and provider cleanup on the owned retirement thread."""
        if not self.agent_terminal:
            close = getattr(self.agent, "close", None)
            try:
                if callable(close):
                    close()
                self.agent_terminal = True
            except Exception:  # noqa: BLE001
                self.failures += 1
                terminal = isinstance(self.agent, RuntimeAgent) and self.agent.closed
                if self.failures == 1:
                    LOGGER.exception(
                        "Failed to close HTTP agent for session %s.",
                        self.owner.session_id,
                    )
                if not terminal:
                    return False
                self.agent_terminal = True
        if self.provider is None:
            self.provider = self.owner._remove_terminal_agent(self.agent)
        if self.provider is not None:
            close_provider = getattr(self.provider, "close", None)
            try:
                if callable(close_provider):
                    close_provider()
            except Exception:  # noqa: BLE001
                self.failures += 1
                if self.failures == 1:
                    LOGGER.exception(
                        "Failed to close an HTTP provider for session %s.",
                        self.owner.session_id,
                    )
                return False
            self.owner._remove_terminal_provider(self.provider)
            self.provider = None
        self.owner._finish_cleanup(self)
        return True


class SessionRuntimeResources:
    """Own mutable runtime resources separately from logical turn state."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_factory: SessionAgentFactory,
        executor: Executor,
        on_process_change: Callable[[], None],
    ) -> None:
        self.session_id = session_id
        self.agent_factory = agent_factory
        self.executor = executor
        self.on_process_change = on_process_change
        self.lock = Lock()
        self._primary_agent: RuntimeAgent | None = None
        self._process_unsubscribe: Callable[[], None] | None = None
        self._session_enabled_tool_names: set[str] | None = None
        self._mcp_session_policy = McpSessionPolicy.empty()
        self._turn_agents: dict[int, object] = {}
        self._providers: dict[int, object] = {}
        self._claimed_providers: set[int] = set()
        self._cleanups: dict[int, _AgentCleanup] = {}
        self._close_completion: Future[None] | None = None
        self._closing = False
        self._closed = False

    def primary_locked(self) -> RuntimeAgent | None:
        """Return the primary agent while the caller holds ``lock``."""
        return self._primary_agent

    def has_primary(self) -> bool:
        """Return whether this owner has built a primary runtime agent."""
        with self.lock:
            return self._primary_agent is not None

    def ensure_primary(
        self,
        record: SessionRecord,
        *,
        load_state: bool,
        active_entries: list[ConversationEntry] | None = None,
        load_active_entries: Callable[[], list[ConversationEntry]] | None = None,
    ) -> object:
        """Build the owned primary and optionally load its active path."""
        with self.lock:
            if self._closing:
                raise RuntimeError("HTTP runtime resources are closing.")
            if self._primary_agent is None:
                candidate = self.agent_factory(record)
                if not isinstance(candidate, RuntimeAgent):
                    return candidate
                self._primary_agent = candidate
                self._providers[id(candidate.provider)] = candidate.provider
                object.__setattr__(
                    candidate.provider,
                    "_yoke_mcp_session_policy",
                    self._mcp_session_policy,
                )
                candidate.refresh_tools(force=True)
                self._process_unsubscribe = candidate.command_process_manager.subscribe(
                    self.on_process_change
                )
                if self._session_enabled_tool_names is not None:
                    candidate.set_session_enabled_tools(
                        self._session_enabled_tool_names
                    )
            primary = self._primary_agent
            assert primary is not None
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

    def prepare_turn(
        self,
        record: SessionRecord,
        *,
        active_entries: list[ConversationEntry] | None,
        load_active_entries: Callable[[], list[ConversationEntry]],
    ) -> object:
        """Create an isolated turn fork with the established runtime projection."""
        primary_or_candidate = self.ensure_primary(record, load_state=False)
        if not isinstance(primary_or_candidate, RuntimeAgent):
            with self.lock:
                if self._closing:
                    raise RuntimeError("HTTP runtime resources are closing.")
                self._turn_agents[id(primary_or_candidate)] = primary_or_candidate
            return primary_or_candidate
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
            primary = self._primary_agent
            if primary is not None and turn_agent is not primary:
                promote_runtime_fork(primary, turn_agent)

    def process_manager(self) -> CommandProcessManager | None:
        """Return the primary agent's process manager when it exists."""
        with self.lock:
            primary = self._primary_agent
            return primary.command_process_manager if primary is not None else None

    def session_enabled_tool_names(self) -> set[str] | None:
        """Return a defensive copy of the process-local tool allowlist."""
        with self.lock:
            return (
                set(self._session_enabled_tool_names)
                if self._session_enabled_tool_names is not None
                else None
            )

    def mcp_session_policy(self) -> McpSessionPolicy:
        """Return a defensive copy of the process-local MCP policy."""
        with self.lock:
            return McpSessionPolicy(servers=dict(self._mcp_session_policy.servers))

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
            existing = self._mcp_session_policy.servers.get(server_name)
            self._mcp_session_policy.servers[server_name] = McpSessionServerPolicy(
                enabled=enabled
                if enabled is not None
                else existing.enabled
                if existing
                else None,
                enabled_tools=(
                    enabled_tools
                    if update_enabled_tools
                    else existing.enabled_tools
                    if existing
                    else None
                ),
                disabled_tools=(
                    disabled_tools
                    if update_disabled_tools
                    else existing.disabled_tools
                    if existing
                    else None
                ),
            )
            primary = self._primary_agent
            if primary is not None:
                object.__setattr__(
                    primary.provider,
                    "_yoke_mcp_session_policy",
                    self._mcp_session_policy,
                )
                primary.refresh_tools(force=True)
                if primary._context is not None:
                    primary._sync_context_instructions(primary._context)

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
            base = (
                set(self._session_enabled_tool_names)
                if self._session_enabled_tool_names is not None
                else set(default_enabled_names)
            )
            next_enabled = (base | enabled) - disabled
            self._session_enabled_tool_names = set(next_enabled)
            primary = self._primary_agent
            if primary is not None:
                hidden_names = set(primary.tools) - discovered_names
                primary.set_session_enabled_tools(next_enabled | hidden_names)
                primary._install_session_filtered_tool_system_messages()
                if primary._context is not None:
                    primary._sync_context_instructions(primary._context)
            return set(next_enabled)

    async def reap(self, turn_agent: object | None) -> None:
        """Join the single daemon cleanup for one completed turn agent."""
        if turn_agent is None:
            return
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
                unsubscribe = self._process_unsubscribe
                self._process_unsubscribe = None
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
                self._maybe_finish_close_locked()
        await asyncio.shield(asyncio.wrap_future(completion))

    def _submit_cleanup_locked(self, agent: object) -> _AgentCleanup:
        cleanup = self._cleanups.get(id(agent))
        if cleanup is not None:
            return cleanup
        cleanup = _AgentCleanup(self, agent)
        self._cleanups[id(agent)] = cleanup
        cleanup.completion = retire_resource(cleanup.attempt)
        return cleanup

    def _remove_terminal_agent(self, agent: object) -> object | None:
        provider = getattr(agent, "provider", None)
        with self.lock:
            if self._primary_agent is agent:
                self._primary_agent = None
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
        agents: list[object] = list(self._turn_agents.values())
        if self._primary_agent is not None:
            agents.append(self._primary_agent)
        return any(getattr(agent, "provider", None) is provider for agent in agents)

    def _remove_terminal_provider(self, provider: object) -> None:
        with self.lock:
            self._providers.pop(id(provider), None)
            self._claimed_providers.discard(id(provider))

    def _finish_cleanup(self, cleanup: _AgentCleanup) -> None:
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
            or self._providers
            or self._cleanups
            or completion is None
        ):
            return
        self._closed = True
        if not completion.done():
            completion.set_result(None)
