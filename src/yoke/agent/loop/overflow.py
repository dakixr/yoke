"""Overflow guards and retry helpers for provider-bound loop iterations."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.usage import compact_usage_payload
from yoke.ai.providers.base import ProviderError


def guard_newest_user_message_images(agent, context: AgentContext) -> None:
    """Reject a newest user message that exceeds provider image limits."""
    newest_message = agent.context_manager.newest_real_user_message(context)
    if newest_message is None:
        return
    max_images = getattr(agent.provider, "max_images_per_message", None)
    image_count = agent.context_manager.message_image_count(newest_message)
    if max_images is not None and image_count > max_images:
        raise ProviderError(
            "Newest user message exceeds provider image limit: "
            f"{image_count} images > {max_images}."
        )


def guard_newest_user_message_tokens(agent, context: AgentContext) -> None:
    """Reject a newest user message that still cannot fit after compaction."""
    newest_message = agent.context_manager.newest_real_user_message(context)
    if newest_message is None:
        return
    instruction_count = len(context.instructions)
    provider_messages = agent.context_manager.messages_for_provider(context)
    newest_provider_messages = [
        *provider_messages[:instruction_count],
        newest_message.model_copy(deep=True),
    ]
    estimate = agent.context_manager.estimate_tokens(newest_provider_messages)
    max_total_tokens = agent.context_manager.compaction_policy.max_total_tokens
    reserved_output_tokens = (
        agent.context_manager.compaction_policy.reserved_output_tokens
    )
    available_input_tokens = max(
        0,
        max_total_tokens - reserved_output_tokens,
    )
    if estimate.input_tokens > available_input_tokens:
        raise ProviderError(
            "Newest user message exceeds remaining provider context budget: "
            f"{estimate.input_tokens} input tokens > {available_input_tokens}."
        )


def should_retry_after_overflow(error: ProviderError) -> bool:
    """Return whether a provider error looks like request overflow."""
    if error.status_code == 413:
        return True
    message = str(error).lower()
    overflow_phrase_found = any(
        phrase in message
        for phrase in (
            "too many image",
            "more than 50img",
            "more than 50 img",
            "context_length_exceeded",
            "context length exceeded",
            "context too long",
            "exceeds the context window",
            "myokemum context length",
            "prompt token count",
            "exceeds the limit",
            "token limit",
            "request too large",
            "too large",
        )
    )
    if error.status_code == 400:
        return overflow_phrase_found
    if error.status_code is None:
        return overflow_phrase_found
    return False


def retry_with_compacted_history(
    agent,
    context: AgentContext,
    *,
    iteration: int,
    on_event: AgentEventHandler | None,
    compact_context,
    emit_event: Callable[[str, dict[str, object]], None],
) -> Message | None:
    """Compact older history, re-append the newest message, and retry."""
    newest_message = agent.context_manager.newest_real_user_message(context)
    if newest_message is None:
        return None
    compacted_context = context.model_copy(deep=True)
    newest_entries = compacted_context.conversation_log.entries
    for index in range(len(newest_entries) - 1, -1, -1):
        entry = newest_entries[index]
        if entry.message is not None and entry.message.role == "user":
            del newest_entries[index]
            break
    compacted_context.messages = agent.context_manager.transcript_messages(
        compacted_context
    )
    compaction = compact_context(
        agent,
        compacted_context,
        iteration=iteration,
        on_event=on_event,
        reason="overflow_retry",
    )
    if compaction.failed:
        return None
    agent.context_manager.append_message(compacted_context, newest_message)
    guard_newest_user_message_images(agent, compacted_context)
    guard_newest_user_message_tokens(agent, compacted_context)
    provider_messages = messages_for_provider_capabilities(
        agent.context_manager.messages_for_provider(compacted_context),
        agent.provider,
    )
    try:
        assistant_message = agent.provider.complete(
            provider_messages,
            agent._tool_definitions(),
        )
    except ProviderError:
        return None
    context.conversation_log = compacted_context.conversation_log.model_copy(deep=True)
    context.memory = compacted_context.memory.model_copy(deep=True)
    context.messages = agent.context_manager.transcript_messages(context)
    emit_event(
        "model_end",
        _model_end_payload(iteration=iteration, message=assistant_message),
    )
    commentary = assistant_message.commentary_text_content()
    if commentary:
        emit_event(
            "assistant_message",
            {
                "iteration": iteration,
                "phase": "commentary",
                "content": commentary,
            },
        )
    return assistant_message


def _model_end_payload(*, iteration: int, message: Message) -> dict[str, object]:
    payload: dict[str, object] = {
        "iteration": iteration,
        "tool_calls": len(message.tool_calls),
        "content": message.text_content() or "",
        "phase": message.phase,
    }
    usage = compact_usage_payload(message.usage)
    if usage is not None:
        payload["usage"] = usage
    return payload
