"""Loop events and terminal results, independent of runtime ownership."""

from __future__ import annotations

from yoke.agent.compaction.types import CompactionPreparation, CompactionResult
from yoke.agent.loop.types import (
    AgentEventHandler,
    AgentResult,
)
from yoke.agent.models import AgentContext, Message
from yoke.agent.usage import compact_usage_payload


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


def emit(
    handler: AgentEventHandler | None,
    event: str,
    payload: dict[str, object],
) -> None:
    """Emit one loop event when a handler is configured."""
    if handler is not None:
        handler(event, payload)
