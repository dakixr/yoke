"""Rendering and truncation helpers for context compaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from yoke.agent.models import Message
from yoke.agent.models import MessageContentPart

if TYPE_CHECKING:
    from yoke.agent.compaction.core import CompactionPreparation


def build_summary_handoff_messages(
    preparation: CompactionPreparation,
) -> list[Message]:
    """Build legacy flattened input, unused by runtime epoch compaction."""
    from yoke.agent.compaction.core import COMPACTION_SUMMARY_PROMPT

    return [
        Message.system(COMPACTION_SUMMARY_PROMPT),
        Message.user(summary_source_text(preparation)),
    ]


def build_compaction_summary_prompt(target_tokens: int) -> str:
    """Build the appended handoff instruction for one target size."""
    if target_tokens <= 0:
        raise ValueError("Compaction handoff target must be positive.")
    from yoke.agent.compaction.core import COMPACTION_SUMMARY_PROMPT
    from yoke.agent.compaction.core import DEFAULT_HANDOFF_TARGET_TOKENS

    return COMPACTION_SUMMARY_PROMPT.replace(
        f"{DEFAULT_HANDOFF_TARGET_TOKENS:,} estimated tokens",
        f"{target_tokens:,} estimated tokens",
        1,
    )


def summary_source_text(preparation: CompactionPreparation) -> str:
    """Render legacy flattened input outside the runtime compaction path."""
    lines = [
        "Summarize this visible transcript for handoff.",
        "",
        "Visible transcript before compaction:",
    ]
    for message in preparation.messages_to_summarize:
        rendered = render_message(message)
        if rendered:
            lines.append(rendered)
    if preparation.boundary == "split_turn" and preparation.turn_prefix_messages:
        lines.extend(["", "Current turn prefix before the kept recent messages:"])
        for message in preparation.turn_prefix_messages:
            rendered = render_message(message)
            if rendered:
                lines.append(rendered)
    return "\n".join(lines).strip()


def render_message(message: Message) -> str:
    """Render a message to a human-readable string for summarization."""
    parts: list[str] = []
    text_content = message.text_content()
    if text_content:
        rendered_content = truncate_text(text_content, limit=600)
        parts.append(f"[{message.role.capitalize()}] {rendered_content}")
    if message.tool_calls:
        calls = [
            f"{tool_call.function.name}("
            f"{truncate_text(tool_call.function.arguments, limit=200)})"
            for tool_call in message.tool_calls
        ]
        parts.append(f"[Assistant tool calls] {'; '.join(calls)}")
    if message.role == "tool" and not message.content:
        parts.append(f"[Tool {message.tool_call_id or 'result'}] (empty result)")
    return "\n".join(parts)


def truncate_text(text: str, *, limit: int) -> str:
    """Truncate and normalize whitespace in text to a character limit."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def is_real_user_message(message: Message) -> bool:
    """Return True if the message is a real user message (not a summary)."""
    if message.role != "user":
        return False
    plain_text = message.plain_text_content
    return not plain_text or parse_memory_safe(plain_text) is None


def parse_memory_safe(content: str) -> str | None:
    """Parse a memory message, returning None if parsing fails."""
    try:
        from yoke.agent.conversation import parse_memory_message

        return parse_memory_message(content)
    except Exception:
        return None


def truncate_message_to_token_budget(
    message: Message,
    *,
    token_budget: int,
) -> Message | None:
    """Truncate a message's content to fit within the given token budget."""
    if token_budget <= 0 or message.content is None:
        return None
    from yoke.agent.compaction.core import TOKEN_WIDTH_GUESS

    char_budget = max(0, (token_budget - 1) * TOKEN_WIDTH_GUESS)
    text_content = message.text_content()
    if not text_content:
        return None
    suffix = (
        "\n\n[Earlier part of this user message was truncated during context "
        "compaction.]"
    )
    usable = max(0, char_budget - len(suffix))
    if usable <= 0:
        return None
    return Message.user(text_content[-usable:].lstrip() + suffix)


def truncate_structured_user_content(
    content: Sequence[MessageContentPart],
    *,
    usable: int,
    suffix: str,
) -> list[MessageContentPart]:
    """Truncate only the text parts of structured user content."""
    from yoke.agent.models import MessageTextContentPart

    remaining = usable
    kept: list[MessageContentPart] = []
    for part in reversed(content):
        if isinstance(part, MessageTextContentPart):
            if remaining <= 0:
                continue
            snippet = part.text[-remaining:].lstrip()
            if snippet:
                kept.append(MessageTextContentPart(text=snippet + suffix))
                remaining = 0
            continue
        kept.append(part.model_copy(deep=True))
    kept.reverse()
    return kept
