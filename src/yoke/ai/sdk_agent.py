"""Public SDK Agent facade."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import StopRequested
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.ai.sdk_types import AgentResult
from yoke.ai.sdk_types import Image
from yoke.ai.sdk_types import RunConfig


class Agent:
    """Public SDK facade for stateful agent prompting."""

    def __init__(self, *, provider: Provider, config: RunConfig) -> None:
        """Create a public SDK agent."""
        from yoke.agent.loop.agent import RuntimeAgent

        self.config = config
        self.root = Path(config.root).resolve()
        self._runtime = RuntimeAgent.from_run_config(
            provider=provider,
            config=config,
        )

    @property
    def provider(self) -> Provider:
        """Return the provider currently used by this agent."""
        return self._runtime.provider

    @provider.setter
    def provider(self, provider: Provider) -> None:
        """Replace the provider and refresh provider-aware tools."""
        self._runtime.provider = provider
        self._runtime.refresh_tools(force=True)

    @property
    def messages(self) -> list[Message]:
        """Return the current transcript messages."""
        return self._runtime.messages

    @property
    def conversation_entries(self) -> list[ConversationEntry]:
        """Return the structured conversation log."""
        return self._runtime.conversation_entries

    @property
    def has_state(self) -> bool:
        """Return whether the agent has conversation state."""
        return self._runtime.has_state

    def reset(self) -> None:
        """Clear conversation state while keeping runtime configuration."""
        self._runtime.reset()

    def fork(self) -> Agent:
        """Fork, creating a new instance with the same configuration."""
        new = Agent(provider=self.provider, config=self.config)
        new._runtime = self._runtime.fork()
        return new

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
        return self._runtime.prompt(
            prompt,
            images=images,
            image_urls=image_urls,
            output_type=output_type,
            on_event=on_event,
            stop_requested=stop_requested,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )
