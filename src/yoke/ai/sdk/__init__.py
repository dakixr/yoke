"""Public SDK helpers for direct completions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.loop.types import ConversationEntryHistory as ConversationEntryHistory
from yoke.agent.loop.types import ConversationHistory as ConversationHistory
from yoke.agent.loop.types import MessageHistory as MessageHistory
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import insert_provider_system_messages
from yoke.ai.providers.base import start_provider_turn
from yoke.ai.sdk.agent import Agent as Agent
from yoke.ai.sdk.artifacts import JsonValue as JsonValue
from yoke.ai.sdk.artifacts import to_jsonable as to_jsonable
from yoke.ai.sdk.artifacts import write_json_artifact as write_json_artifact
from yoke.ai.sdk.batch import run_many as run_many
from yoke.ai.sdk.capabilities import CapabilityInfo as CapabilityInfo
from yoke.ai.sdk.capabilities import discover_capabilities as discover_capabilities
from yoke.ai.sdk.defaults import (
    default_coding_agent_config as default_coding_agent_config,
)
from yoke.ai.sdk.defaults import (
    default_coding_agent_tools as default_coding_agent_tools,
)
from yoke.ai.sdk.types import BatchItemResult as BatchItemResult
from yoke.ai.sdk.types import BatchProgress as BatchProgress
from yoke.ai.sdk.types import BatchResult as BatchResult
from yoke.ai.sdk.types import BatchTask as BatchTask
from yoke.ai.sdk.types import BatchUsage as BatchUsage
from yoke.ai.sdk.types import AgentNotCompletedError as AgentNotCompletedError
from yoke.ai.sdk.types import AgentResult as AgentResult
from yoke.ai.sdk.types import CompletionResult
from yoke.ai.sdk.types import Context
from yoke.ai.sdk.types import Image
from yoke.ai.sdk.types import RunConfig as RunConfig
from yoke.ai.sdk.types import Skill as Skill
from yoke.ai.sdk.types import (
    StructuredOutputError as StructuredOutputError,
)
from yoke.ai.sdk.types import append_structured_output_instructions
from yoke.ai.sdk.types import build_user_message_from_images
from yoke.ai.sdk.types import normalize_image_inputs
from yoke.ai.sdk.types import parse_structured_output
from yoke.ai.sdk.types import structured_output_instructions
from yoke.ai.sdk.types import structured_output_retry_message

STRUCTURED_OUTPUT_MAX_ATTEMPTS = 3


def complete[StructuredT](
    prompt: str | None = None,
    *,
    provider: Provider,
    context: Context | None = None,
    messages: list[Message] | None = None,
    sys_prompt: str | None = None,
    images: Sequence[Image | str | Path] = (),
    image_urls: Sequence[str] = (),
    output_type: type[StructuredT] | None = None,
) -> CompletionResult[StructuredT]:
    """Run one direct completion against a provider."""
    normalized_images, normalized_urls = normalize_image_inputs(
        images=images,
        image_urls=image_urls,
    )
    resolved_messages = _build_messages(
        prompt=prompt,
        context=context,
        messages=messages,
        sys_prompt=sys_prompt,
        images=normalized_images,
        image_urls=normalized_urls,
    )
    resolved_messages = insert_provider_system_messages(
        resolved_messages,
        provider,
    )
    if output_type is not None:
        resolved_messages = _with_structured_output_instructions(
            resolved_messages,
            output_type=output_type,
        )
    attempts = 1 if output_type is None else STRUCTURED_OUTPUT_MAX_ATTEMPTS
    response: Message | None = None
    output = ""
    structured: StructuredT | None = None
    last_error: StructuredOutputError | None = None
    for attempt in range(attempts):
        start_provider_turn(provider)
        response = provider.complete(resolved_messages, [])
        output = response.final_text_content() or ""
        try:
            structured = parse_structured_output(output, output_type=output_type)
            break
        except StructuredOutputError as exc:
            last_error = exc
            if output_type is None or attempt == attempts - 1:
                continue
            resolved_messages.extend(
                [response, structured_output_retry_message(output_type, exc)]
            )
    else:
        if last_error is not None:
            raise last_error
    if response is None:
        raise RuntimeError("Provider did not return a response.")
    return CompletionResult(
        message=response,
        output=output,
        messages=[*resolved_messages, response],
        structured=structured,
    )


def _build_messages(
    *,
    prompt: str | None,
    context: Context | None,
    messages: list[Message] | None,
    sys_prompt: str | None,
    images: Sequence[Image] = (),
    image_urls: Sequence[str] = (),
) -> list[Message]:
    """Normalize SDK inputs into one message history."""
    if context is not None:
        resolved = [message.model_copy(deep=True) for message in context.messages]
        if prompt is not None or images or image_urls:
            resolved.append(
                build_user_message_from_images(
                    prompt or "", images=images, image_urls=image_urls
                )
            )
        return resolved
    resolved = [message.model_copy(deep=True) for message in messages or []]
    if sys_prompt and not any(message.role == "system" for message in resolved):
        resolved.insert(0, Message.system(sys_prompt))
    if prompt is not None or images or image_urls:
        resolved.append(
            build_user_message_from_images(
                prompt or "", images=images, image_urls=image_urls
            )
        )
    if not resolved:
        raise ValueError("Provide prompt, context, or messages.")
    return resolved


def _with_structured_output_instructions(
    messages: list[Message],
    *,
    output_type: type[object],
) -> list[Message]:
    """Attach structured-output requirements to the request transcript."""
    resolved = [message.model_copy(deep=True) for message in messages]
    for index in range(len(resolved) - 1, -1, -1):
        message = resolved[index]
        if message.role == "user":
            resolved[index] = append_structured_output_instructions(
                message,
                output_type=output_type,
            )
            return resolved
    resolved.append(Message.user(structured_output_instructions(output_type)))
    return resolved
