"""Iteration lifecycle helpers for the agent loop."""

from __future__ import annotations

from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import CompactionReason
from yoke.agent.compaction import CompactionResult
from yoke.agent.loop.compaction_summary import summarize_compaction
from yoke.agent.loop.overflow import guard_newest_user_message_images
from yoke.agent.loop.overflow import retry_with_compacted_history
from yoke.agent.loop.overflow import should_retry_after_overflow
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import AgentResult
from yoke.agent.loop.types import AgentStoppedError
from yoke.agent.loop.types import CompactionAttempt
from yoke.agent.loop.types import INTERRUPTED_TURN_NOTICE
from yoke.agent.loop.types import StopRequested
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.usage import compact_usage_payload
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import complete_with_cancel


def handle_pre_model_compaction(
    agent,
    context: AgentContext,
    iteration: int,
    on_event: AgentEventHandler | None,
) -> bool:
    """Compact before the provider call when needed."""
    compaction = compact_context_for_iteration(
        agent,
        context,
        iteration=iteration,
        on_event=on_event,
    )
    if compaction.failed:
        return True
    if compaction.result is not None:
        emit_context_usage(
            agent,
            on_event,
            context,
            iteration=iteration,
            reason="compaction",
        )
    return False


def complete_iteration_model(
    agent,
    context: AgentContext,
    *,
    iteration: int,
    on_event: AgentEventHandler | None,
    stop_requested: StopRequested | None,
) -> Message:
    """Run one provider completion call."""
    guard_newest_user_message_images(agent, context)
    provider_messages = messages_for_provider_capabilities(
        agent.context_manager.messages_for_provider(context), agent.provider
    )
    emit(
        on_event,
        "model_start",
        {"iteration": iteration, "message_count": len(provider_messages)},
    )
    try:
        assistant_message = complete_with_cancel(
            agent.provider,
            provider_messages,
            agent._tool_definitions(),
            cancel_requested=stop_requested,
        )
        for skill in context.active_skills:
            if skill.is_inline:
                continue
            skill.reload_on_next_use = False
    except ProviderCancelledError as exc:
        raise AgentStoppedError() from exc
    except ProviderError as exc:
        if should_retry_after_overflow(exc):
            recovered = retry_with_compacted_history(
                agent,
                context,
                iteration=iteration,
                on_event=on_event,
                compact_context=compact_context_for_iteration,
                emit_event=lambda event, payload: emit(on_event, event, payload),
            )
            if recovered is not None:
                return recovered
        exc.partial_messages = context.messages
        raise
    emit(
        on_event,
        "model_end",
        model_end_payload(iteration=iteration, message=assistant_message),
    )
    commentary = assistant_message.commentary_text_content()
    if commentary:
        emit(
            on_event,
            "assistant_message",
            {
                "iteration": iteration,
                "phase": "commentary",
                "content": commentary,
            },
        )
    return assistant_message


def model_end_payload(*, iteration: int, message: Message) -> dict[str, object]:
    """Build a model_end event payload."""
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


def handle_post_tool_results(
    agent,
    context: AgentContext,
    iteration: int,
    on_event: AgentEventHandler | None,
) -> None:
    """Refresh state and compact after tool results when needed."""
    agent.active_skills = [
        skill.model_copy(deep=True) for skill in context.active_skills
    ]
    emit_context_usage(
        agent,
        on_event,
        context,
        iteration=iteration,
        reason="tool_results",
    )
    compaction = compact_context_for_iteration(
        agent,
        context,
        iteration=iteration,
        on_event=on_event,
        after_tool_results=True,
    )
    if compaction.failed:
        raise AgentStoppedError()
    if compaction.result is not None:
        emit_context_usage(
            agent,
            on_event,
            context,
            iteration=iteration,
            reason="post_tool_compaction",
        )


def sync_runtime_skills_from_context(agent, context: AgentContext) -> None:
    """Mirror active skill state from the working context back to the agent."""
    agent.active_skills = [
        skill.model_copy(deep=True) for skill in context.active_skills
    ]


def completed_result(
    context: AgentContext,
    *,
    output: str,
    iterations: int,
    on_event: AgentEventHandler | None,
) -> AgentResult:
    """Build the final completed result."""
    emit(
        on_event,
        "iteration_end",
        {"iteration": iterations, "stop_reason": "assistant"},
    )
    return AgentResult(
        output=output,
        messages=context.messages,
        iterations=iterations,
        status="completed",
        conversation_entries=[
            entry.model_copy(deep=True) for entry in context.conversation_log.entries
        ],
    )


def compact_context_for_iteration(
    agent,
    context: AgentContext,
    *,
    iteration: int,
    on_event: AgentEventHandler | None,
    after_tool_results: bool = False,
    reason: CompactionReason = "threshold",
) -> CompactionAttempt:
    """Attempt context compaction for one iteration."""
    preparation = (
        agent.context_manager.prepare_post_tool_compaction(context)
        if after_tool_results
        else agent.context_manager.prepare_compaction(context, reason=reason)
    )
    if preparation is None:
        return CompactionAttempt()
    summary_text = summarize_compaction(
        agent,
        preparation,
        context=context,
        on_event=on_event,
        emit=emit,
    )
    if summary_text is None:
        emit_compaction_failed(
            on_event,
            preparation,
            iteration=iteration,
        )
        return CompactionAttempt(failed=True, preparation=preparation)
    result = agent.context_manager.apply_compaction(
        context,
        preparation,
        summary_text=summary_text,
    )
    compacted_estimate = agent.context_manager.estimate_tokens(result.messages)
    emit_compaction_event(
        on_event,
        preparation,
        result,
        compacted_input_tokens=compacted_estimate.input_tokens,
        iteration=iteration,
    )
    return CompactionAttempt(result=result, preparation=preparation)


def emit_compaction_event(
    handler: AgentEventHandler | None,
    preparation: CompactionPreparation,
    result: CompactionResult,
    *,
    compacted_input_tokens: int,
    iteration: int,
) -> None:
    """Emit a successful compaction event."""
    emit(
        handler,
        "context_compaction",
        {
            "iteration": iteration,
            "reason": preparation.reason,
            "boundary": preparation.boundary,
            "summarized_messages": len(preparation.messages_to_summarize),
            "kept_messages": len(preparation.kept_messages),
            "message_count": len(result.messages),
            "input_tokens": preparation.estimate.input_tokens,
            "compacted_input_tokens": compacted_input_tokens,
            "total_tokens": preparation.estimate.total_with_reserve,
        },
    )


def emit_compaction_failed(
    handler: AgentEventHandler | None,
    preparation: CompactionPreparation,
    *,
    iteration: int,
) -> None:
    """Emit a failed compaction event."""
    emit(
        handler,
        "context_compaction_failed",
        {
            "iteration": iteration,
            "reason": preparation.reason,
            "boundary": preparation.boundary,
            "summarized_messages": len(preparation.messages_to_summarize),
            "kept_messages": len(preparation.kept_messages),
        },
    )


def emit_context_usage(
    agent,
    handler: AgentEventHandler | None,
    context: AgentContext,
    *,
    iteration: int,
    reason: str,
) -> None:
    """Emit estimated provider context usage."""
    if handler is None:
        return
    provider_messages = agent.context_manager.messages_for_provider(context)
    accounting = agent.context_manager.account_tokens(provider_messages)
    payload: dict[str, object] = {
        "iteration": iteration,
        "reason": reason,
        "message_count": len(provider_messages),
        "input_tokens": accounting.input_tokens,
        "total_with_reserve": accounting.total_with_reserve,
        "estimated_input_tokens": accounting.estimated_input_tokens,
        "estimated_total_with_reserve": (accounting.estimated_total_with_reserve),
        "accounting_source": accounting.source,
    }
    if accounting.provider_reported_input_tokens is not None:
        payload["provider_reported_input_tokens"] = (
            accounting.provider_reported_input_tokens
        )
    if accounting.output_tokens is not None:
        payload["output_tokens"] = accounting.output_tokens
    if accounting.reasoning_tokens is not None:
        payload["reasoning_tokens"] = accounting.reasoning_tokens
    if accounting.total_tokens is not None:
        payload["provider_reported_total_tokens"] = accounting.total_tokens
    if accounting.cached_input_tokens is not None:
        payload["cached_input_tokens"] = accounting.cached_input_tokens
    max_total_tokens = agent.context_manager.max_total_tokens
    if isinstance(max_total_tokens, int) and max_total_tokens > 0:
        payload["max_total_tokens"] = max_total_tokens
        payload["usage_percent"] = min(
            100,
            max(0, round((accounting.input_tokens / max_total_tokens) * 100)),
        )
    emit(handler, "context_usage", payload)


def stopped_result(
    context: AgentContext,
    *,
    iterations: int,
    append_message,
) -> AgentResult:
    """Build the standard stopped result payload."""
    append_message(context, Message.assistant(INTERRUPTED_TURN_NOTICE))
    return AgentResult(
        output="Stopped current turn.",
        messages=context.messages,
        iterations=iterations,
        status="stopped",
        conversation_entries=[
            entry.model_copy(deep=True) for entry in context.conversation_log.entries
        ],
    )


def emit(
    handler: AgentEventHandler | None,
    event: str,
    payload: dict[str, object],
) -> None:
    """Emit one loop event when a handler is configured."""
    if handler is not None:
        handler(event, payload)
