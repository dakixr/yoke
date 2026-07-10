"""Public SDK Agent facade."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
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
        rebind_context_manager_budget(
            self._runtime.context_manager,
            provider=provider,
            policy_override=self.config.compaction,
        )
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
        new = object.__new__(Agent)
        new.config = self.config
        new.root = self.root
        new._runtime = self._runtime.fork()
        return new

    def close(self) -> None:
        """Release resources owned by the underlying runtime."""
        self._runtime.close()

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
        from pydantic import BaseModel

        from yoke.observe import current_workflow

        run = current_workflow()
        node_id: str | None = None
        if run is not None:
            node_id = run.agent_node_for(
                agent=self,
                label=self.__class__.__name__,
                metadata={
                    "agent": _agent_metadata(
                        provider=self.provider,
                        prompt=prompt,
                        output_type=output_type,
                    ),
                    "source": _agent_callsite(),
                },
            )
            run.emit(
                "agent_event",
                node_id=node_id,
                payload={
                    "event": "prompt_started",
                    "prompt": prompt[:4000],
                    "prompt_truncated": len(prompt) > 4000,
                    "output_type": output_type.__name__
                    if output_type is not None
                    else None,
                },
            )

        def observed_event(event: str, payload: dict[str, object]) -> None:
            if run is not None and node_id is not None:
                run.emit(
                    "agent_event",
                    node_id=node_id,
                    payload={"event": event, **payload},
                )
            if on_event is not None:
                on_event(event, payload)

        try:
            result = self._runtime.prompt(
                prompt,
                images=images,
                image_urls=image_urls,
                output_type=output_type,
                on_event=observed_event if run is not None else on_event,
                stop_requested=stop_requested,
                before_tool_call=before_tool_call,
                after_tool_call=after_tool_call,
            )
        except BaseException as exc:
            if run is not None and node_id is not None:
                run.emit(
                    "agent_event",
                    node_id=node_id,
                    payload={
                        "event": "prompt_failed",
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            raise

        if run is not None and node_id is not None:
            if isinstance(result.structured, BaseModel):
                run.remember_output(node_id, result.structured)
            run.emit(
                "agent_event",
                node_id=node_id,
                payload={
                    "event": "prompt_completed",
                    "output_type": output_type.__name__
                    if output_type is not None
                    else None,
                },
            )
        return result


def _agent_metadata(
    *,
    provider: Provider,
    prompt: str,
    output_type: type[object] | None,
) -> dict[str, object]:
    config = getattr(provider, "config", None)
    model = getattr(config, "model", None)
    reasoning_effort = getattr(config, "reasoning_effort", None)
    provider_name = getattr(provider, "provider_name", provider.__class__.__name__)
    metadata: dict[str, object] = {
        "provider": str(provider_name),
        "prompt": prompt[:4000],
        "prompt_truncated": len(prompt) > 4000,
        "output_type": output_type.__name__ if output_type is not None else None,
    }
    if isinstance(model, str):
        metadata["model"] = model
    if isinstance(reasoning_effort, str):
        metadata["reasoning_effort"] = reasoning_effort
    return metadata


def _agent_callsite() -> dict[str, object]:
    current_file = Path(__file__).resolve()
    for frame in inspect.stack()[2:]:
        try:
            frame_path = Path(frame.filename).resolve()
        except OSError:
            frame_path = Path(frame.filename)
        if frame_path == current_file:
            continue
        return {
            "path": frame.filename,
            "line": frame.lineno,
            "function": frame.function,
            "code": (frame.code_context or [""])[0].strip(),
        }
    return {}
