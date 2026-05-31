"""Compaction summarizer request helpers for the runtime loop."""

from __future__ import annotations

import time

from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import build_summary_handoff_messages
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry


def summarize_compaction(
    agent,
    preparation: CompactionPreparation,
    *,
    context: AgentContext | None = None,
    on_event: AgentEventHandler | None = None,
    emit,
) -> str | None:
    """Ask the provider to summarize the compaction window."""
    summarizer_messages = build_summary_handoff_messages(preparation)
    estimated_input_tokens = preparation.estimate.input_tokens
    emit(
        on_event,
        "compaction_summary_start",
        {"estimated_input_tokens": estimated_input_tokens},
    )
    start_time = time.perf_counter()
    try:
        response = agent.provider.complete(summarizer_messages, [])
    except Exception as exc:
        metadata: dict[str, object] = {
            "ok": False,
            "estimated_input_tokens": estimated_input_tokens,
            "duration_seconds": round(time.perf_counter() - start_time, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }
        _record_summary_attempt(
            context=context,
            on_event=on_event,
            emit=emit,
            metadata=metadata,
        )
        return None
    summary = (response.plain_text_content or "").strip()
    metadata: dict[str, object] = {
        "ok": bool(summary),
        "estimated_input_tokens": estimated_input_tokens,
        "duration_seconds": round(time.perf_counter() - start_time, 2),
    }
    if summary:
        metadata["response_chars"] = len(summary)
    else:
        metadata["error"] = "empty_summary"
    _record_summary_attempt(
        context=context,
        on_event=on_event,
        emit=emit,
        metadata=metadata,
    )
    return summary or None


def _record_summary_attempt(
    *,
    context: AgentContext | None,
    on_event: AgentEventHandler | None,
    emit,
    metadata: dict[str, object],
) -> None:
    emit(on_event, "compaction_summary_end", metadata)
    if context is None:
        return
    parent_id = (
        context.conversation_log.entries[-1].id
        if context.conversation_log.entries
        else None
    )
    context.conversation_log.entries.append(
        ConversationEntry(
            kind="compaction_summary",
            parent_id=parent_id,
            metadata=metadata,
        )
    )
