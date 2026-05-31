"""Public SDK helpers for direct completions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.ai.sdk_agent import Agent as Agent
from yoke.ai.sdk_types import AgentResult as AgentResult
from yoke.ai.sdk_types import CompletionResult
from yoke.ai.sdk_types import Context
from yoke.ai.sdk_types import Image
from yoke.ai.sdk_types import RunConfig as RunConfig
from yoke.ai.sdk_types import Skill as Skill
from yoke.ai.sdk_types import (
    StructuredOutputError as StructuredOutputError,
)
from yoke.ai.sdk_types import append_structured_output_instructions
from yoke.ai.sdk_types import build_user_message_from_images
from yoke.ai.sdk_types import normalize_image_inputs
from yoke.ai.sdk_types import parse_structured_output
from yoke.ai.sdk_types import structured_output_instructions


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
    if output_type is not None:
        resolved_messages = _with_structured_output_instructions(
            resolved_messages,
            output_type=output_type,
        )
    response = provider.complete(resolved_messages, [])
    output = response.final_text_content() or ""
    return CompletionResult(
        message=response,
        output=output,
        messages=[*resolved_messages, response],
        structured=parse_structured_output(
            output,
            output_type=output_type,
        ),
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
