"""Agent compaction operations used by runtimes and applications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from typing import cast

from yoke.agent.compaction.core import CompactionPreparation
from yoke.agent.compaction.core import CompactionResult
from yoke.agent.compaction.core import TokenEstimate
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.lifecycle import compact_context_for_iteration
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.agent.usage import effective_usage_accounting


@dataclass(slots=True, frozen=True)
class ForcedCompaction:
    """Result of a manually forced agent compaction."""

    messages: list[Message]
    preparation: CompactionPreparation
    result: CompactionResult
    conversation_entries: list[ConversationEntry]
    provider_messages: list[Message]
    compacted_estimate: TokenEstimate


def force_compact_agent(
    agent: object,
    messages: list[Message],
    *,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> ForcedCompaction | None:
    """Force a compaction turn and return updated structured state."""
    if not isinstance(agent, RuntimeAgent):
        return None
    context_manager = agent.context_manager
    context = context_manager.initialize(
        "/compact",
        messages,
        conversation_entries=conversation_entries,
        available_skills=agent.available_skills,
        active_skills=agent.active_skills,
    )
    if (
        context.messages
        and context.messages[-1].role == "user"
        and context.messages[-1].plain_text_content == "/compact"
    ):
        context.conversation_log.entries.pop()
        context.messages = context_manager.transcript_messages(context)
    compaction = compact_context_for_iteration(
        agent,
        context,
        iteration=0,
        on_event=None,
        reason="manual",
    )
    if compaction.result is None or compaction.preparation is None:
        return None
    agent._context = context
    provider_messages = messages_for_provider_capabilities(
        context_manager.messages_for_provider(context), agent.provider
    )
    compacted_estimate = context_manager.estimate_tokens(provider_messages)
    return ForcedCompaction(
        messages=context.messages,
        preparation=compaction.preparation,
        result=compaction.result,
        conversation_entries=[
            entry.model_copy(deep=True) for entry in context.conversation_log.entries
        ],
        provider_messages=provider_messages,
        compacted_estimate=compacted_estimate,
    )


def estimate_agent_context_usage(
    agent: object,
    prompt: str,
    messages: list[Message],
    *,
    conversation_entries: Sequence[ConversationEntry] | None = None,
) -> dict[str, Any] | None:
    """Estimate current prompt context usage against provider budget."""
    if not isinstance(agent, RuntimeAgent) and not hasattr(agent, "context_manager"):
        return None
    context_manager = getattr(agent, "context_manager", None)
    if context_manager is None:
        return None
    max_total_tokens = getattr(context_manager, "max_total_tokens", None)
    if not isinstance(max_total_tokens, int) or max_total_tokens <= 0:
        return None
    context = context_manager.initialize(
        prompt,
        messages,
        append_prompt=bool(prompt),
        conversation_entries=conversation_entries,
        available_skills=cast(
            Sequence[object] | None, getattr(agent, "available_skills", None)
        ),
        active_skills=cast(
            Sequence[object] | None, getattr(agent, "active_skills", None)
        ),
    )
    estimate = context_manager.estimate_tokens(
        messages_for_provider_capabilities(
            context_manager.messages_for_provider(context),
            getattr(agent, "provider", None),
        )
    )
    accounting = effective_usage_accounting(
        estimate,
        latest_usage=_latest_entry_usage(context.conversation_log.entries),
    )
    usage_percent = min(
        100,
        max(0, round((accounting.input_tokens / max_total_tokens) * 100)),
    )
    payload = {
        "input_tokens": accounting.input_tokens,
        "max_total_tokens": max_total_tokens,
        "usage_percent": usage_percent,
        "total_with_reserve": accounting.total_with_reserve,
        "estimated_input_tokens": accounting.estimated_input_tokens,
        "estimated_total_with_reserve": (accounting.estimated_total_with_reserve),
        "accounting_source": accounting.source,
    }
    if accounting.provider_reported_input_tokens is not None:
        payload["provider_reported_input_tokens"] = (
            accounting.provider_reported_input_tokens
        )
    if accounting.reasoning_tokens is not None:
        payload["reasoning_tokens"] = accounting.reasoning_tokens
    return payload


def _latest_entry_usage(
    entries: Sequence[ConversationEntry],
) -> TokenUsage | None:
    for entry in reversed(entries):
        if entry.message is None or entry.message.usage is None:
            continue
        if entry.message.usage.input_tokens is not None:
            return entry.message.usage
    return None
