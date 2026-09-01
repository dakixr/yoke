"""Markdown rendering for portable session handoffs."""

from __future__ import annotations

from .models import SessionHandoff
from .models import SessionHandoffMessage


def render_session_handoff_markdown(handoff: SessionHandoff) -> str:
    """Render a handoff as Markdown that can be fed directly to another agent."""
    lines = ["# Yoke session handoff", ""]
    lines.append(f"- Session: `{handoff.session_id}`")
    if handoff.title:
        lines.append(f"- Title: {handoff.title}")
    if handoff.root:
        lines.append(f"- Working directory: `{handoff.root}`")
    selection = _selection_text(handoff)
    if selection:
        lines.append(f"- Model selection: `{selection}`")
    if handoff.updated_at:
        lines.append(f"- Updated: `{handoff.updated_at}`")
    if handoff.leaf_id:
        lines.append(f"- Active branch leaf: `{handoff.leaf_id}`")
    if handoff.active_skills:
        lines.append(
            f"- Active skills: {', '.join(f'`{name}`' for name in handoff.active_skills)}"
        )
    context_line = (
        f"- Context source: {handoff.retained_entries} active-branch entries retained "
        f"from a {handoff.total_entries}-entry session"
    )
    if handoff.omitted_messages:
        context_line += (
            f", {handoff.omitted_messages} rendered messages omitted by the size bound"
        )
    lines.append(context_line)
    if handoff.truncated:
        lines.append(
            "- Note: this handoff is bounded. Prefer the retained recent context and compaction summary."
        )
    lines.extend(
        [
            "",
            "Use this as prior work context. Continue from the active branch rather than replaying the session.",
            "",
            "## Conversation",
            "",
        ]
    )
    for message in handoff.messages:
        lines.extend(render_handoff_message(message))
    return "\n".join(lines).rstrip() + "\n"


def render_handoff_message(message: SessionHandoffMessage) -> list[str]:
    """Render one portable handoff message block."""
    heading = _message_heading(message)
    lines = [f"### {heading}", ""]
    if message.content:
        if message.role == "tool":
            lines.extend([_fenced(message.content, "text"), ""])
        else:
            lines.extend([message.content, ""])
    for image in message.images:
        image_line = f"- Image: {image.label}"
        if image.source:
            image_line += f" at `{image.source}`"
        lines.append(image_line)
    if message.images:
        lines.append("")
    if message.tool_calls:
        lines.append("Tool calls:")
        for call in message.tool_calls:
            lines.append(f"- `{call.name}` (`{call.id}`)")
            if call.arguments:
                lines.append(_fenced(call.arguments, "json"))
        lines.append("")
    if message.truncated:
        lines.extend(["_[Content truncated for handoff size.]_", ""])
    return lines


def _message_heading(message: SessionHandoffMessage) -> str:
    if message.source == "compaction_summary":
        return "Compacted history"
    if message.source == "compaction_retained":
        prefix = "Retained context"
    elif message.role == "user":
        prefix = "User"
    elif message.role == "assistant" and message.phase == "commentary":
        prefix = "Assistant commentary"
    elif message.role == "assistant":
        prefix = "Assistant"
    elif message.role == "tool":
        prefix = "Tool result"
    else:
        prefix = message.role.capitalize()
    if message.role == "tool" and message.tool_call_id:
        return f"{prefix} `{message.tool_call_id}`"
    return prefix


def _selection_text(handoff: SessionHandoff) -> str | None:
    if not handoff.provider_name and not handoff.model_id:
        return None
    selection = ":".join(
        value for value in (handoff.provider_name, handoff.model_id) if value
    )
    if handoff.reasoning_effort:
        selection += f" / {handoff.reasoning_effort}"
    return selection


def _fenced(value: str, language: str) -> str:
    longest = 0
    current = 0
    for char in value:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{value}\n{fence}"
