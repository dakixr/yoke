"""Execution implementation for the public SDK Agent facade."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import StopRequested
from yoke.agent.models import Message
from yoke.ai.sdk.observability import AgentObserver
from yoke.ai.sdk.observability import compose_event_handler
from yoke.ai.sdk.observability import notify_observers
from yoke.ai.sdk.prompting import build_prompt_message
from yoke.ai.sdk.prompting import to_agent_result
from yoke.ai.sdk.structured import structured_output_retry_message
from yoke.ai.sdk.types import AgentResult
from yoke.ai.sdk.types import Image
from yoke.ai.sdk.types import StructuredOutputError
from yoke.ai.providers.usage_context import usage_metric_context

if TYPE_CHECKING:
    from yoke.ai.sdk.agent import Agent

STRUCTURED_OUTPUT_MAX_ATTEMPTS = 3


def run_agent_prompt[StructuredT](
    agent: Agent,
    prompt: str,
    *,
    images: Sequence[Image | str | Path],
    image_urls: Sequence[str],
    output_type: type[StructuredT] | None,
    on_event: AgentEventHandler | None,
    observer: AgentObserver | None,
    stop_requested: StopRequested | None,
    before_tool_call: BeforeToolCallHook | None,
    after_tool_call: AfterToolCallHook | None,
) -> AgentResult[StructuredT]:
    """Run one prompt while the caller owns the agent prompt lock."""
    observers = tuple(item for item in (agent.observer, observer) if item is not None)
    event_handler = compose_event_handler(on_event, observers)
    notify_observers(observers, "agent_start", {"prompt": prompt})
    try:
        result = _run_attempts(
            agent,
            prompt,
            images=images,
            image_urls=image_urls,
            output_type=output_type,
            on_event=event_handler,
            stop_requested=stop_requested,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )
        if agent._autosave:
            if agent._state_path is None:
                raise RuntimeError("Autosave agent lost its bound state path")
            agent._save_unlocked(agent._state_path)
    except BaseException as exc:
        notify_observers(
            observers,
            "agent_error",
            {"error": str(exc), "error_type": type(exc).__name__},
        )
        raise
    notify_observers(
        observers,
        "agent_end",
        {
            "output": result.output,
            "status": result.status,
            "iterations": result.iterations,
        },
    )
    return result


def _run_attempts[StructuredT](
    agent: Agent,
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
    user_message = build_prompt_message(
        prompt,
        images=images,
        image_urls=image_urls,
        output_type=output_type,
    )
    attempts = 1 if output_type is None else STRUCTURED_OUTPUT_MAX_ATTEMPTS
    last_error: StructuredOutputError | None = None
    result: AgentResult[StructuredT] | None = None
    next_prompt = prompt
    next_user_message = user_message
    retry_instructions: list[Message] = []
    try:
        for attempt in range(attempts):
            retry_context = (
                usage_metric_context(call_kind="structured_output_retry")
                if attempt > 0
                else nullcontext()
            )
            with retry_context:
                runtime_result = agent._runtime.run(
                    next_prompt,
                    user_message=next_user_message,
                    on_event=on_event,
                    stop_requested=stop_requested,
                    before_tool_call=before_tool_call,
                    after_tool_call=after_tool_call,
                )
            try:
                result = to_agent_result(runtime_result, output_type=output_type)
                break
            except StructuredOutputError as exc:
                last_error = exc
                if output_type is None or attempt == attempts - 1:
                    continue
                retry_message = structured_output_retry_message(output_type, exc)
                if agent._runtime._context is not None:
                    agent._runtime._context.instructions.append(retry_message)
                    retry_instructions.append(retry_message)
                next_prompt = "Retry with corrected structured output."
                next_user_message = Message.user(next_prompt)
        else:
            if last_error is not None:
                raise last_error
    finally:
        if agent._runtime._context is not None and retry_instructions:
            instructions = agent._runtime._context.instructions
            retry_ids = {id(item) for item in retry_instructions}
            instructions[:] = [
                item for item in instructions if id(item) not in retry_ids
            ]
    if result is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("Agent did not return a result.")
    return result
