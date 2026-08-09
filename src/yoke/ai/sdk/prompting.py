"""Prompt message construction and public result conversion."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.loop.types import AgentResult as RuntimeAgentResult
from yoke.agent.models import Message
from yoke.ai.sdk.messages import build_user_message_from_images
from yoke.ai.sdk.messages import normalize_image_inputs
from yoke.ai.sdk.structured import append_structured_output_instructions
from yoke.ai.sdk.structured import parse_structured_output
from yoke.ai.sdk.types import AgentResult
from yoke.ai.sdk.types import Image


def build_prompt_message[StructuredT](
    prompt: str,
    *,
    images: Sequence[Image | str | Path],
    image_urls: Sequence[str],
    output_type: type[StructuredT] | None,
) -> Message | None:
    """Build an explicit user message when SDK options require one."""
    normalized_images, normalized_urls = normalize_image_inputs(
        images=images,
        image_urls=image_urls,
    )
    user_message = None
    if normalized_images or normalized_urls:
        user_message = build_user_message_from_images(
            prompt,
            images=normalized_images,
            image_urls=normalized_urls,
        )
    if output_type is not None:
        base_message = user_message or Message.user(prompt)
        user_message = append_structured_output_instructions(
            base_message,
            output_type=output_type,
        )
    return user_message


def to_agent_result[StructuredT](
    runtime_result: RuntimeAgentResult,
    *,
    output_type: type[StructuredT] | None,
) -> AgentResult[StructuredT]:
    """Convert a runtime result into the public SDK result type."""
    structured = parse_structured_output(
        runtime_result.output,
        output_type=output_type,
    )
    messages = [message.model_copy(deep=True) for message in runtime_result.messages]
    if messages:
        message = messages[-1]
    else:
        message = Message.assistant(runtime_result.output)
    return AgentResult(
        message=message,
        output=runtime_result.output,
        messages=messages,
        iterations=runtime_result.iterations,
        status=runtime_result.status,
        conversation_entries=(
            None
            if runtime_result.conversation_entries is None
            else [
                entry.model_copy(deep=True)
                for entry in runtime_result.conversation_entries
            ]
        ),
        structured=structured,
    )
