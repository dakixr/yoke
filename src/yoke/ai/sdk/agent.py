"""Public SDK Agent facade."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import partial
import os
from pathlib import Path
import threading
from uuid import uuid4

from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import StopRequested
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.persistence import restore_agent_state
from yoke.ai.sdk.async_support import drain_worker
from yoke.ai.sdk.async_support import run_sync_cooperatively
from yoke.ai.sdk.durable import DurableAgentMixin
from yoke.ai.sdk.durable import normalize_state_path
from yoke.ai.sdk.observability import AgentObserver
from yoke.ai.sdk.prompt_runner import run_agent_prompt
from yoke.ai.sdk.resources import CloseAttempt
from yoke.ai.sdk.resources import ProviderLease
from yoke.ai.providers.base import Provider
from yoke.ai.providers.usage_context import (
    current_usage_metric_context,
)
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.ai.sdk.defaults import default_coding_agent_config
from yoke.ai.sdk.types import AgentResult
from yoke.ai.sdk.types import Image
from yoke.ai.sdk.types import RunConfig


class Agent(DurableAgentMixin):
    """Public SDK facade for stateful agent prompting."""

    def __init__(
        self,
        *,
        provider: Provider,
        config: RunConfig | None = None,
        state_path: str | os.PathLike[str] | None = None,
        autosave: bool = False,
        observer: AgentObserver | None = None,
    ) -> None:
        """Create a public SDK agent."""
        from yoke.ai.sdk.runtime import build_runtime_agent

        if autosave and state_path is None:
            raise ValueError("autosave=True requires state_path.")
        if config is None:
            config = default_coding_agent_config()
        self.provider = provider
        self._provider_lease = ProviderLease.claim(provider)
        self.config = config
        self.root = Path(config.root).resolve()
        self._state_path = normalize_state_path(state_path)
        self._autosave = autosave
        self.observer = observer
        self._prompt_lock = threading.RLock()
        self._async_prompt_lock: asyncio.Lock | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_loop_lock = threading.Lock()
        self._prompt_owner: int | None = None
        self._state_lock = threading.Lock()
        self._close_attempt: CloseAttempt | None = None
        self._closing = False
        self._closed = False
        try:
            self._runtime = build_runtime_agent(
                provider=provider,
                config=config,
            )
            if self._state_path is not None and self._state_path.exists():
                restore_agent_state(self._runtime, self._state_path)
        except BaseException:
            runtime = getattr(self, "_runtime", None)
            try:
                if runtime is not None:
                    runtime.close()
            finally:
                self._provider_lease.release()
            raise

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        provider: Provider,
        config: RunConfig | None = None,
        autosave: bool = False,
        strict: bool = True,
        observer: AgentObserver | None = None,
    ) -> Agent:
        """Create an agent by loading durable state from a snapshot file."""
        agent = cls(provider=provider, config=config, observer=observer)
        try:
            agent.restore(path, strict=strict)
        except BaseException:
            agent.close()
            raise
        agent._state_path = normalize_state_path(path)
        agent._autosave = autosave
        return agent

    @property
    def messages(self) -> list[Message]:
        """Return the current transcript messages."""
        with self._prompt_lock:
            return [message.model_copy(deep=True) for message in self._runtime.messages]

    @property
    def conversation_entries(self) -> list[ConversationEntry]:
        """Return the structured conversation log."""
        with self._prompt_lock:
            return [
                entry.model_copy(deep=True)
                for entry in self._runtime.conversation_entries
            ]

    @property
    def has_state(self) -> bool:
        """Return whether the agent has conversation state."""
        with self._prompt_lock:
            return self._runtime.has_state

    def reset(self) -> None:
        """Clear conversation state while keeping runtime configuration."""
        with self._prompt_lock:
            self._ensure_open()
            self._ensure_not_prompt_callback("reset")
            self._runtime.reset()

    def close(self) -> None:
        """Release provider and tool resources owned by this agent."""
        if self._prompt_owner == threading.get_ident():
            raise RuntimeError("Cannot close an agent from its prompt callback")
        with self._state_lock:
            if self._closed:
                return
            attempt = self._close_attempt
            if attempt is None:
                attempt = CloseAttempt()
                self._close_attempt = attempt
                self._closing = True
                owns_attempt = True
            else:
                owns_attempt = False
        if not owns_attempt:
            if attempt.owner_thread_id == threading.get_ident():
                return
            attempt.wait()
            return

        error: BaseException | None = None
        runtime_terminal = False
        try:
            with self._prompt_lock:
                try:
                    self._runtime.close()
                except BaseException as exc:
                    error = exc
                    runtime_terminal = self._runtime.closed
                else:
                    runtime_terminal = True
                if runtime_terminal:
                    try:
                        self._provider_lease.release()
                    except BaseException as exc:
                        if error is None:
                            error = exc
        except BaseException as exc:
            if error is None:
                error = exc
        finally:
            with self._state_lock:
                self._closed = runtime_terminal
                self._closing = not runtime_terminal
                self._close_attempt = None
                attempt.finish(error)
        if error is not None:
            raise error

    @property
    def closed(self) -> bool:
        """Return whether this agent has released its resources."""
        with self._state_lock:
            return self._closed

    async def aclose(self) -> None:
        """Release owned resources without blocking the event loop."""
        if self._prompt_owner == threading.get_ident():
            raise RuntimeError("Cannot close an agent from its prompt callback")
        with self._state_lock:
            attempt = self._close_attempt
            if attempt is not None and attempt.owner_thread_id == threading.get_ident():
                return
        worker = asyncio.create_task(asyncio.to_thread(self.close))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            await drain_worker(worker)
            raise

    async def __aenter__(self) -> Agent:
        """Return this agent from an async context manager."""
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object | None,
    ) -> None:
        """Close this agent when leaving an async context manager."""
        await self.aclose()

    def fork(self) -> Agent:
        """Fork state while preventing duplicate shared-provider ownership."""
        with self._prompt_lock:
            self._ensure_open()
            self._ensure_not_prompt_callback("fork")
            runtime = self._runtime.fork(isolate_provider=True)
            new = object.__new__(Agent)
            new.provider = runtime.provider
            new.config = self.config
            new.root = self.root
            new._state_path = None
            new._autosave = False
            new.observer = self.observer
            new._prompt_lock = threading.RLock()
            new._async_prompt_lock = None
            new._async_loop = None
            new._async_loop_lock = threading.Lock()
            new._prompt_owner = None
            new._state_lock = threading.Lock()
            new._close_attempt = None
            new._closing = False
            new._closed = False
            new._provider_lease = (
                self._provider_lease.acquire()
                if runtime.provider is self.provider
                else ProviderLease.claim(runtime.provider)
            )
            new._runtime = runtime
            return new

    def prompt[StructuredT](
        self,
        prompt: str,
        *,
        images: Sequence[Image | str | Path] = (),
        image_urls: Sequence[str] = (),
        output_type: type[StructuredT] | None = None,
        on_event: AgentEventHandler | None = None,
        observer: AgentObserver | None = None,
        stop_requested: StopRequested | None = None,
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
    ) -> AgentResult[StructuredT]:
        """Prompt the agent through the runtime SDK flow."""
        with self._prompt_lock:
            self._ensure_open()
            if self._prompt_owner is not None:
                raise RuntimeError("Recursive prompts on one agent are not supported")
            self._prompt_owner = threading.get_ident()
            try:
                usage_context = current_usage_metric_context()
                with usage_metric_context(
                    surface="sdk",
                    sdk_operation=usage_context.sdk_operation or "agent",
                    sdk_run_id=usage_context.sdk_run_id or uuid4().hex,
                ):
                    return run_agent_prompt(
                        self,
                        prompt,
                        images=images,
                        image_urls=image_urls,
                        output_type=output_type,
                        on_event=on_event,
                        observer=observer,
                        stop_requested=stop_requested,
                        before_tool_call=before_tool_call,
                        after_tool_call=after_tool_call,
                    )
            finally:
                self._prompt_owner = None

    async def prompt_async[StructuredT](
        self,
        prompt: str,
        *,
        images: Sequence[Image | str | Path] = (),
        image_urls: Sequence[str] = (),
        output_type: type[StructuredT] | None = None,
        on_event: AgentEventHandler | None = None,
        observer: AgentObserver | None = None,
        stop_requested: StopRequested | None = None,
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
        timeout: float | None = None,
    ) -> AgentResult[StructuredT]:
        """Prompt asynchronously with cooperative timeout and cancellation."""
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        prompt_lock = self._async_lock_for_current_loop()
        cancelled = threading.Event()

        def combined_stop() -> bool:
            return cancelled.is_set() or bool(
                stop_requested is not None and stop_requested()
            )

        call = partial(
            self.prompt,
            prompt,
            images=images,
            image_urls=image_urls,
            output_type=output_type,
            on_event=on_event,
            observer=observer,
            stop_requested=combined_stop,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )
        if timeout is None:
            async with prompt_lock:
                return await run_sync_cooperatively(
                    call,
                    timeout=None,
                    stop_event=cancelled,
                )
        async with asyncio.timeout(timeout):
            async with prompt_lock:
                return await run_sync_cooperatively(
                    call,
                    timeout=None,
                    stop_event=cancelled,
                )

    def _async_lock_for_current_loop(self) -> asyncio.Lock:
        """Bind asynchronous use to one event loop."""
        loop = asyncio.get_running_loop()
        with self._async_loop_lock:
            if self._async_loop is None:
                self._async_loop = loop
                self._async_prompt_lock = asyncio.Lock()
            elif self._async_loop is not loop:
                raise RuntimeError("One Agent cannot be used from multiple event loops")
            if self._async_prompt_lock is None:
                raise RuntimeError("Agent async prompt lock was not initialized")
            return self._async_prompt_lock

    def _ensure_open(self) -> None:
        """Reject operations that require resources after closure."""
        with self._state_lock:
            if self._closing or self._closed:
                raise RuntimeError("Agent is closing or closed")

    def _ensure_not_prompt_callback(self, operation: str) -> None:
        """Reject state mutation re-entered from a running prompt callback."""
        if self._prompt_owner == threading.get_ident():
            raise RuntimeError(f"Cannot {operation} an agent from its prompt callback")
