"""Lazy process-wide registry for HTTP-owned Yoke session runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from threading import Lock
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypeVar

from yoke.http.models.session import ActiveRuntimeInfo
from yoke.http.errors import ApiError
from yoke.http.services.event_broker import EventService
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.runtime_factory import generate_http_session_title
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
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TitleGenerationResult:
    status: Literal["empty", "generated"]
    title: str | None = None


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
        self._executor_closed = False

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

    def cancel_automatic_title(self, session_id: str) -> None:
        """Cancel automatic naming before an explicit title mutation."""
        runtime = self.get_if_loaded(session_id)
        if runtime is not None:
            runtime.cancel_automatic_title()

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
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self.executor,
            self._regenerate_title_sync,
            session_id,
        )
        if result.status == "empty":
            raise ApiError(
                400,
                "title_regeneration_unavailable",
                "Session has no conversation to title.",
            )
        if result.title is None:
            raise ApiError(
                502,
                "title_regeneration_failed",
                "Could not generate a session title.",
            )
        return result.title

    def _regenerate_title_sync(self, session_id: str) -> _TitleGenerationResult:
        record = self.read_cache.get(session_id).record
        if not record.messages:
            return _TitleGenerationResult(status="empty")
        return _TitleGenerationResult(
            status="generated",
            title=generate_http_session_title(
                self.agent_factory,
                record,
                record.messages,
            ),
        )

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
        """Close all runtimes, then stop their shared worker executor."""
        await self.close_runtimes(grace_seconds=grace_seconds)
        self.shutdown_executor()

    async def close_runtimes(self, *, grace_seconds: float = 5.0) -> None:
        """Give loaded runtime controllers bounded grace and log timeouts.

        Running provider calls are ordinary Python threads and cannot be force
        terminated. Their retained outcomes and resources retire on the daemon
        cleanup path if those calls return after this method's grace expires.
        This method can return first, but Python interpreter shutdown may still
        wait for a non-cooperative runtime-executor thread to return.
        """
        with self._lock:
            runtimes = list(self._runtimes.items())
            self._runtimes.clear()
        if not runtimes:
            return
        tasks = {
            asyncio.create_task(
                runtime.close(),
                name=f"yoke-http-runtime-close-{session_id}",
            ): session_id
            for session_id, runtime in runtimes
        }
        done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in done:
            if task.cancelled():
                LOGGER.warning("Closing HTTP runtime %s was cancelled.", tasks[task])
                continue
            error = task.exception()
            if error is not None:
                LOGGER.error(
                    "Failed to close HTTP runtime %s.",
                    tasks[task],
                    exc_info=(type(error), error, error.__traceback__),
                )
        if pending:
            LOGGER.warning(
                "Timed out closing %d HTTP runtime(s): %s",
                len(pending),
                ", ".join(sorted(tasks[task] for task in pending)),
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def shutdown_executor(self) -> None:
        """Reject queued work without claiming running provider threads stopped."""
        with self._lock:
            if self._executor_closed:
                return
            self._executor_closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
