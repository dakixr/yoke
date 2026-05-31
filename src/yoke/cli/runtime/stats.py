"""Conversation statistics helpers for yoke CLI runtime."""

from __future__ import annotations

from yoke.agent.compaction import Compactor
from yoke.agent.compaction import TokenEstimate
from yoke.agent.models import Message


def estimate_messages_token_usage(messages: list[Message]) -> TokenEstimate:
    """Estimate token usage using the shared compaction estimator."""
    return Compactor().estimate_tokens(messages, reserve_tokens=0)


def conversation_stats(messages: list[Message]) -> dict[str, object]:
    """Summarize message-role and token statistics."""
    role_counts = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    tool_call_count = 0
    total_chars = 0
    for message in messages:
        role_counts[message.role] = role_counts.get(message.role, 0) + 1
        text_content = message.text_content()
        if text_content:
            total_chars += len(text_content)
        tool_call_count += len(message.tool_calls)
    estimated_tokens = estimate_messages_token_usage(messages).input_tokens
    return {
        "message_count": len(messages),
        "role_counts": role_counts,
        "tool_call_count": tool_call_count,
        "estimated_tokens": estimated_tokens,
        "total_chars": total_chars,
    }
