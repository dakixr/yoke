"""Public SDK Agent facade."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import StopRequested
from yoke.agent.budget import rebind_context_manager_budget
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.ai.sdk.types import AgentResult
from yoke.ai.sdk.types import Image
from yoke.ai.sdk.types import RunConfig
from yoke.ai.sdk.async_support import run_sync_cooperatively
from yoke.ai.sdk.defaults import default_coding_agent_config
from yoke.ai.sdk.durable import DurableAgentMixin
from yoke.ai.sdk.durable import normalize_state_path
from yoke.ai.sdk.resources import ProviderLease
from yoke.ai.sdk.types import StructuredOutputError
from yoke.ai.sdk.types import structured_output_retry_message

STRUCTURED_OUTPUT_MAX_ATTEMPTS = 3


class Agent(DurableAgentMixin):
    """Public SDK facade for stateful agent prompting."""

    def __init__(
        self,
        *,
        provider: Provider,
        config: RunConfig | None = None,
        state_path: str | os.PathLike[str] | None = None,
        autosave: bool = False,
    ) -> None:
        """Create a public SDK agent."""
        from yoke.agent.loop.agent import RuntimeAgent

        if config is None:
            config = default_coding_agent_config()
        if autosave and state_path is None:
            raise ValueError("autosave=True requires state_path.")
        self.config = config
        self.root = Path(config.root).resolve()
        self._provider_lease = ProviderLease(provider)
        self._state_path = normalize_state_path(state_path)
        self._autosave = autosave
        self._prompt_lock = threading.RLock()
        self._async_prompt_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._closed_event = threading.Event()
        self._prompt_owner: int | None = None
        self._closing = False
        self._closed = False
        try:
            self._runtime = RuntimeAgent.from_run_config(
                provider=provider,
                config=config,
            )
            if self._state_path is not None and self._state_path.exists():
                from yoke.agent.persistence import restore_agent_state

                restore_agent_state(
                    self._runtime,
                    self._state_path,
                    available_skills=list(self._runtime.available_skills),
                )
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
    ) -> Agent:
        """Create an agent by loading durable state from a snapshot file."""
        agent = cls(provider=provider, config=config)
        try:
            agent.restore(path, strict=strict)
        except BaseException:
            agent.close()
            raise
        agent._state_path = normalize_state_path(path)
        agent._autosave = autosave
        return agent

    @property
    def provider(self) -> Provider:
        """Return the provider currently used by this agent."""
        return self._runtime.provider

    @provider.setter
    def provider(self, provider: Provider) -> None:
        """Replace the provider and refresh provider-aware tools."""
        with self._prompt_lock:
            self._ensure_open()
            self._ensure_not_prompt_callback("replace provider on")
            if provider is self._runtime.provider:
                return
            old_lease = self._provider_lease
            self._runtime.provider = provider
            self._provider_lease = ProviderLease(provider)
            try:
                rebind_context_manager_budget(
                    self._runtime.context_manager,
                    provider=provider,
                    policy_override=self.config.compaction,
                )
                self._runtime.refresh_tools(force=True)
            finally:
                old_lease.release()

    @property
    def messages(self) -> list[Message]:
        """Return the current transcript messages."""
        with self._prompt_lock:
            return self._runtime.messages

    @property
    def conversation_entries(self) -> list[ConversationEntry]:
        """Return the structured conversation log."""
        with self._prompt_lock:
            return self._runtime.conversation_entries

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

    def fork(self) -> Agent:
        """Fork, creating a new instance with the same configuration."""
        with self._prompt_lock:
            self._ensure_open()
            self._ensure_not_prompt_callback("fork")
            runtime = self._runtime.fork(isolate_provider=True)
            new = object.__new__(Agent)
            new.config = self.config
            new.root = self.root
            new._state_path = None
            new._autosave = False
            new._prompt_lock = threading.RLock()
            new._async_prompt_lock = asyncio.Lock()
            new._state_lock = threading.Lock()
            new._closed_event = threading.Event()
            new._prompt_owner = None
            new._closing = False
            new._closed = False
            new._provider_lease = (
                self._provider_lease.acquire()
                if runtime.provider is self.provider
                else ProviderLease(runtime.provider)
            )
            new._runtime = runtime
            return new

    def close(self) -> None:
        """Release resources owned by the underlying runtime."""
        if self._prompt_owner == threading.get_ident():
            raise RuntimeError("Cannot close an agent from its prompt callback")
        with self._state_lock:
            if self._closed:
                return
            wait_for_close = self._closing
            self._closing = True
        if wait_for_close:
            self._closed_event.wait()
            return
        with self._prompt_lock:
            try:
                self._runtime.close()
            finally:
                try:
                    self._provider_lease.release()
                finally:
                    with self._state_lock:
                        self._closed = True
                    self._closed_event.set()

    @property
    def closed(self) -> bool:
        """Return whether this agent has released runtime resources."""
        with self._state_lock:
            return self._closed

    async def aclose(self) -> None:
        """Release runtime resources without blocking the event loop."""
        await asyncio.to_thread(self.close)

    def __enter__(self) -> Agent:
        """Return this agent from a synchronous context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close this agent when leaving a synchronous context manager."""
        del exc_type, exc, traceback
        self.close()

    async def __aenter__(self) -> Agent:
        """Return this agent from an async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close this agent when leaving an async context manager."""
        del exc_type, exc, traceback
        await self.aclose()

    def prompt[StructuredT](
        self,
        prompt: str,
        *,
        images: Sequence[Image | str | Path] = (),
        image_urls: Sequence[str] = (),
        output_type: type[StructuredT] | None = None,
        on_event: AgentEventHandler | None = None,
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
                return self._prompt_unlocked(
                    prompt,
                    images=images,
                    image_urls=image_urls,
                    output_type=output_type,
                    on_event=on_event,
                    stop_requested=stop_requested,
                    before_tool_call=before_tool_call,
                    after_tool_call=after_tool_call,
                )
            finally:
                self._prompt_owner = None

    def _prompt_unlocked[StructuredT](
        self,
        prompt: str,
        *,
        images: Sequence[Image | str | Path],
        image_urls: Sequence[str],
        output_type: type[StructuredT] | None,
        on_event: AgentEventHandler | None,
        stop_requested: StopRequested | None,
        before_tool_call: BeforeToolCallHook | None,
        after_tool_call: AfterToolCallHook | None,
    ) -> AgentResult[StructuredT]:
        """Run one prompt with structured-output retries and autosave."""
        attempts = 1 if output_type is None else STRUCTURED_OUTPUT_MAX_ATTEMPTS
        last_error: StructuredOutputError | None = None
        result: AgentResult[StructuredT] | None = None
        next_prompt = prompt
        next_images = images
        next_image_urls = image_urls
        retry_instructions: list[Message] = []
        try:
            for attempt in range(attempts):
                try:
                    result = self._runtime.prompt(
                        next_prompt,
                        images=next_images,
                        image_urls=next_image_urls,
                        output_type=output_type,
                        on_event=on_event,
                        stop_requested=stop_requested,
                        before_tool_call=before_tool_call,
                        after_tool_call=after_tool_call,
                    )
                    break
                except StructuredOutputError as exc:
                    last_error = exc
                    if output_type is None or attempt == attempts - 1:
                        continue
                    retry_message = structured_output_retry_message(output_type, exc)
                    self._runtime._base_instructions.append(retry_message)
                    retry_instructions.append(retry_message)
                    next_prompt = "Retry with corrected structured output."
                    next_images = ()
                    next_image_urls = ()
            else:
                if last_error is not None:
                    raise last_error
        finally:
            if retry_instructions:
                retry_ids = {id(message) for message in retry_instructions}
                self._runtime._base_instructions[:] = [
                    message
                    for message in self._runtime._base_instructions
                    if id(message) not in retry_ids
                ]
                self._runtime.refresh_tools(force=True)
        if result is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("Agent did not return a result.")
        if self._autosave and result.completed:
            if self._state_path is None:
                raise RuntimeError("Autosave agent lost its bound state path")
            self._save_unlocked(self._state_path)
        return result

    async def prompt_async[StructuredT](
        self,
        prompt: str,
        *,
        images: Sequence[Image | str | Path] = (),
        image_urls: Sequence[str] = (),
        output_type: type[StructuredT] | None = None,
        on_event: AgentEventHandler | None = None,
        stop_requested: StopRequested | None = None,
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
        timeout: float | None = None,
    ) -> AgentResult[StructuredT]:
        """Prompt asynchronously with cooperative cancellation and timeout."""
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        cancelled = threading.Event()

        def combined_stop() -> bool:
            return cancelled.is_set() or bool(
                stop_requested is not None and stop_requested()
            )

        prompt_call = partial(
            self.prompt,
            prompt,
            images=images,
            image_urls=image_urls,
            output_type=output_type,
            on_event=on_event,
            stop_requested=combined_stop,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )

        def call() -> AgentResult[StructuredT]:
            result = prompt_call()
            if cancelled.is_set() and result.status == "stopped":
                raise RuntimeError("Async prompt stopped after cancellation")
            return result

        async def run() -> AgentResult[StructuredT]:
            async with self._async_prompt_lock:
                return await run_sync_cooperatively(call, stop_event=cancelled)

        if timeout is None:
            return await run()
        async with asyncio.timeout(timeout):
            return await run()

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closing or self._closed:
                raise RuntimeError("Agent is closing or closed")

    def _ensure_not_prompt_callback(self, operation: str) -> None:
        if self._prompt_owner == threading.get_ident():
            raise RuntimeError(f"Cannot {operation} an agent from its prompt callback")
