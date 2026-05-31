"""Compaction and context-usage helpers for yoke CLI runtime."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import CompactionResult
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.compaction import estimate_agent_context_usage
from yoke.agent.compaction import force_compact_agent

from yoke.cli.runtime.base import AgentRunner


def force_compact_history(
    agent: AgentRunner,
    messages: list[Message],
    *,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> (
    tuple[
        list[Message],
        CompactionPreparation,
        CompactionResult,
        list[ConversationEntry],
        dict[str, object],
        dict[str, object],
    ]
    | None
):
    """Force a compaction turn and return CLI display payloads."""
    compacted = force_compact_agent(
        agent,
        messages,
        conversation_entries=conversation_entries,
    )
    if compacted is None:
        return None
    preparation = compacted.preparation
    result = compacted.result
    compacted_estimate = compacted.compacted_estimate
    compaction_payload: dict[str, object] = {
        "iteration": 0,
        "reason": preparation.reason,
        "boundary": preparation.boundary,
        "summarized_messages": len(preparation.messages_to_summarize),
        "kept_messages": len(preparation.kept_messages),
        "message_count": len(result.messages),
        "input_tokens": preparation.estimate.input_tokens,
        "compacted_input_tokens": compacted_estimate.input_tokens,
        "total_tokens": preparation.estimate.total_with_reserve,
    }
    usage_payload: dict[str, object] = {
        "iteration": 0,
        "reason": preparation.reason,
        "message_count": len(compacted.provider_messages),
        "input_tokens": compacted_estimate.input_tokens,
        "total_with_reserve": compacted_estimate.total_with_reserve,
    }
    max_total_tokens = getattr(
        getattr(agent, "context_manager", None), "max_total_tokens", None
    )
    if isinstance(max_total_tokens, int) and max_total_tokens > 0:
        usage_payload["max_total_tokens"] = max_total_tokens
        usage_payload["usage_percent"] = min(
            100,
            max(
                0,
                round((compacted_estimate.input_tokens / max_total_tokens) * 100),
            ),
        )
    return (
        compacted.messages,
        preparation,
        result,
        compacted.conversation_entries,
        compaction_payload,
        usage_payload,
    )


def estimate_context_usage(
    agent: object,
    prompt: str,
    messages: list[Message],
    *,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> dict[str, int] | None:
    """Estimate current prompt context usage against provider budget."""
    return estimate_agent_context_usage(
        agent,
        prompt,
        messages,
        conversation_entries=conversation_entries,
    )
