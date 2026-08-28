"""Lazy process-wide registry for HTTP-owned Yoke session runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import TYPE_CHECKING
from typing import TypeVar

from yoke.http.models.session import ActiveRuntimeInfo
from yoke.http.errors import ApiError
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.session import SessionRecord
from yoke.session import SessionStore

if TYPE_CHECKING:
    from yoke.agent.provider_selection import ProviderSessionState
    from yoke.agent.skills.models import ActiveSkill
    from yoke.http.services.runtime import SessionRuntime


T = TypeVar("T")
type SessionAgentFactory = Callable[[SessionRecord], object]


class SessionRuntimeRegistry:
    """Load session runtimes lazily and coordinate process-wide concurrency."""

    def __init__(
        self,
        *,
        store: SessionStore,
        pending_inputs: PendingInputService,
        events: EventService,
        agent_factory: SessionAgentFactory,
        read_cache: SessionReadCache | None = None,
        message_index: SessionMessageIndex | None = None,
        indexed_runtime_seed: bool = False,
        max_active_sessions: int = 4,
        max_worker_threads: int | None = None,
    ) -> None:
        if max_active_sessions < 1:
            raise ValueError("max_active_sessions must be positive.")
        workers = max_worker_threads or max(8, max_active_sessions * 4)
        if workers < max_active_sessions:
            raise ValueError("max_worker_threads cannot be below max_active_sessions.")
        self.store = store
        self.pending_inputs = pending_inputs
        self.events = events
        self.agent_factory = agent_factory
        self.read_cache = read_cache or SessionReadCache(store)
        self.message_index = message_index
        self.indexed_runtime_seed = indexed_runtime_seed
        self.active_slots = asyncio.Semaphore(max_active_sessions)
        self.executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="yoke-http-turn",
        )
        self._lock = Lock()
        self._runtimes: dict[str, SessionRuntime] = {}

    def get_or_start(self, session_id: str) -> SessionRuntime:
        """Return one lazy runtime without starting model work."""
        with self._lock:
            runtime = self._runtimes.get(session_id)
            if runtime is None:
                from yoke.http.services.runtime import SessionRuntime

                runtime = SessionRuntime(
                    session_id,
                    store=self.store,
                    pending_inputs=self.pending_inputs,
                    events=self.events,
                    agent_factory=self.agent_factory,
                    read_cache=self.read_cache,
                    message_index=self.message_index,
                    indexed_runtime_seed=self.indexed_runtime_seed,
                    executor=self.executor,
                    active_slots=self.active_slots,
                )
                self._runtimes[session_id] = runtime
            return runtime

    def get_if_loaded(self, session_id: str) -> SessionRuntime | None:
        """Return a loaded runtime without instantiating one."""
        with self._lock:
            return self._runtimes.get(session_id)

    def loaded_runtimes(self) -> list[tuple[str, SessionRuntime]]:
        """Return a shallow process-local runtime registry snapshot."""
        with self._lock:
            return list(self._runtimes.items())

    async def wake(self, session_id: str) -> None:
        await self.get_or_start(session_id).wake()

    async def interrupt(self, session_id: str) -> tuple[bool, int | None]:
        runtime = self.get_if_loaded(session_id)
        if runtime is None:
            return False, None
        return await runtime.interrupt()

    async def wait(
        self,
        session_id: str,
        timeout_seconds: float | None = None,
    ) -> ActiveRuntimeInfo:
        runtime = self.get_if_loaded(session_id)
        if runtime is None:
            return ActiveRuntimeInfo(state="idle")
        return await runtime.wait(timeout_seconds)

    async def active_snapshot(self) -> dict[str, ActiveRuntimeInfo]:
        """Return loaded non-idle runtime activity without loading saved sessions."""
        runtimes = self.loaded_runtimes()
        result: dict[str, ActiveRuntimeInfo] = {}
        for session_id, runtime in runtimes:
            status = await runtime.status()
            if status.state != "idle":
                result[session_id] = status
        return result

    async def idle_mutation(self, session_id: str, mutation: Callable[[], T]) -> T:
        """Serialize an idle-only domain mutation against prompt wake/steering."""
        return await self.get_or_start(session_id).idle_mutation(mutation)

    async def select_model(
        self,
        session_id: str,
        *,
        provider_name: str,
        model_id: str,
        reasoning_effort: str | None,
    ) -> ProviderSessionState:
        """Apply one provider/model selection through the session controller."""
        return await self.get_or_start(session_id).select_model(
            provider_name=provider_name,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )

    async def compact(self, session_id: str) -> str:
        """Schedule manual compaction for one session."""
        return await self.get_or_start(session_id).compact()

    async def regenerate_title(self, session_id: str) -> str:
        """Generate a fresh title from the persisted conversation."""
        record = self.read_cache.get(session_id).record
        if not record.messages:
            raise ApiError(
                400,
                "title_regeneration_unavailable",
                "Session has no conversation to title.",
            )
        loop = asyncio.get_running_loop()
        generated = await loop.run_in_executor(
            self.executor,
            self._regenerate_title_sync,
            session_id,
        )
        if generated is None:
            raise ApiError(
                502,
                "title_regeneration_failed",
                "Could not generate a session title.",
            )
        return generated

    def _regenerate_title_sync(self, session_id: str) -> str | None:
        from yoke.session.title import generate_session_title

        record = self.read_cache.get(session_id).record
        agent = self.agent_factory(record)
        try:
            return generate_session_title(agent, record.messages)
        finally:
            close = getattr(agent, "close", None)
            if callable(close):
                close()

    async def activate_skill(self, session_id: str, skill_name: str) -> ActiveSkill:
        """Activate one skill through the session controller."""
        return await self.get_or_start(session_id).activate_skill(skill_name)

    async def set_tools(
        self,
        session_id: str,
        *,
        discovered_names: set[str],
        default_enabled_names: set[str],
        enabled: set[str],
        disabled: set[str],
    ) -> set[str]:
        """Apply a session-only tool allowlist."""
        return await self.get_or_start(session_id).set_tools(
            discovered_names=discovered_names,
            default_enabled_names=default_enabled_names,
            enabled=enabled,
            disabled=disabled,
        )

    async def set_mcp_policy(
        self,
        session_id: str,
        server_name: str,
        *,
        enabled: bool | None,
        enabled_tools: tuple[str, ...] | None,
        disabled_tools: tuple[str, ...] | None,
        update_enabled_tools: bool = False,
        update_disabled_tools: bool = False,
    ) -> None:
        """Apply a session-local MCP policy override."""
        await self.get_or_start(session_id).set_mcp_policy(
            server_name,
            enabled=enabled,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
            update_enabled_tools=update_enabled_tools,
            update_disabled_tools=update_disabled_tools,
        )

    async def close(self, *, grace_seconds: float = 5.0) -> None:
        """Signal every runtime and wait only for a bounded logical grace period."""
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        if runtimes:
            try:
                async with asyncio.timeout(grace_seconds):
                    await asyncio.gather(
                        *(runtime.close() for runtime in runtimes),
                        return_exceptions=True,
                    )
            except TimeoutError:
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)
