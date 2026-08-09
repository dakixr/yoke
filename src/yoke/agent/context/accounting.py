"""Context usage and persisted message metadata helpers."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.agent.usage import compact_usage_payload


def latest_message_usage(messages: Sequence[Message]) -> TokenUsage | None:
    """Return the newest provider-reported usage in a message sequence."""
    for message in reversed(messages):
        if message.usage is not None and message.usage.input_tokens is not None:
            return message.usage
    return None


def latest_log_usage(
    entries: Sequence[ConversationEntry],
) -> TokenUsage | None:
    """Return usage after the newest active checkpoint, if present."""
    for entry in reversed(entries):
        if entry.kind == "memory_snapshot":
            return None
        if entry.message is None:
            continue
        usage = entry.message.usage
        if usage is not None and usage.input_tokens is not None:
            return usage
    return None


def message_entry_metadata(message: Message) -> dict[str, object]:
    """Return compact persisted metadata for one message entry."""
    usage = compact_usage_payload(message.usage)
    return {} if usage is None else {"usage": usage}
