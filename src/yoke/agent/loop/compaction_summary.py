"""Append-only compaction handoff generation."""

from __future__ import annotations

import time
from dataclasses import dataclass

from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import build_compaction_summary_prompt
from yoke.agent.context.helpers import append_conversation_entry
from yoke.agent.loop.cache_scope import conversation_cache_scope
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderRequestContext
from yoke.ai.providers.base import complete_with_cancel
from yoke.ai.providers.usage_context import usage_metric_context


@dataclass(frozen=True, slots=True)
class CompactionSummary:
    """The appended control instruction and generated assistant handoff."""

    instruction: Message
    response: Message


def summarize_compaction(
    agent,
    preparation: CompactionPreparation,
    *,
    context: AgentContext | None = None,
    on_event: AgentEventHandler | None = None,
    emit,
) -> CompactionSummary | None:
    """Generate a handoff by extending the current provider epoch once."""
    estimated_input_tokens = preparation.estimate.input_tokens
    emit(
        on_event,
        "compaction_summary_start",
        {"estimated_input_tokens": estimated_input_tokens},
    )
    target_tokens = agent.context_manager.compaction_policy.handoff_target_tokens
    instruction = Message.user(build_compaction_summary_prompt(target_tokens))
    messages = [
        *[
            message.model_copy(deep=True)
            for message in preparation.messages_to_summarize
        ],
        instruction.model_copy(deep=True),
    ]
    provider_messages = messages_for_provider_capabilities(
        messages,
        agent.provider,
    )
    start_time = time.perf_counter()
    try:
        with usage_metric_context(call_kind="compaction_summary"):
            response = complete_with_cancel(
                agent.provider,
                provider_messages,
                agent._tool_definitions(),
                request_context=ProviderRequestContext(
                    cache_scope=(
                        conversation_cache_scope(context)
                        if context is not None
                        else "empty-conversation"
                    )
                ),
            )
        summary = (response.plain_text_content or "").strip()
        if not summary:
            raise ValueError("Compaction provider returned an empty summary.")
    except Exception as exc:
        metadata: dict[str, object] = {
            "ok": False,
            "estimated_input_tokens": estimated_input_tokens,
            "duration_seconds": round(time.perf_counter() - start_time, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }
        _record_failed_attempt(
            context=context,
            instruction=instruction,
            on_event=on_event,
            emit=emit,
            metadata=metadata,
        )
        return None
    metadata: dict[str, object] = {
        "ok": True,
        "estimated_input_tokens": estimated_input_tokens,
        "duration_seconds": round(time.perf_counter() - start_time, 2),
        "source_chunks": 1,
        "summary_calls": 1,
        "response_chars": len(summary),
        "handoff_target_tokens": target_tokens,
    }
    emit(on_event, "compaction_summary_end", metadata)
    return CompactionSummary(instruction=instruction, response=response)


def _record_failed_attempt(
    *,
    context: AgentContext | None,
    instruction: Message,
    on_event: AgentEventHandler | None,
    emit,
    metadata: dict[str, object],
) -> None:
    emit(on_event, "compaction_summary_end", metadata)
    if context is None:
        return
    append_conversation_entry(
        context,
        ConversationEntry(
            kind="compaction_summary",
            message=instruction,
            metadata=metadata,
        ),
    )
