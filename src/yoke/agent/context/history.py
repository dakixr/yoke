"""Stateless transcript mutations and views over canonical context history."""

from __future__ import annotations

import json

from yoke.agent.context.accounting import message_entry_metadata
from yoke.agent.context.helpers import (
    append_conversation_entry,
    entry_kind_for_message,
    update_message_projection,
)
from yoke.agent.conversation import project_conversation
from yoke.agent.models import (
    AgentContext,
    ConversationEntry,
    Message,
    MessageImageURLContentPart,
    MessageLocalImageContentPart,
)
from yoke.agent.skills.context import (
    skill_message_conversation_entry,
    skill_name_from_message,
)


def append_message(context: AgentContext, message: Message) -> None:
    """Append a message to the context's conversation log."""
    copied = message.model_copy(deep=True)
    branching = append_conversation_entry(
        context,
        ConversationEntry(
            kind=entry_kind_for_message(copied),
            message=copied,
            metadata=message_entry_metadata(copied),
        ),
    )
    update_message_projection(context, copied, branching=branching)


def append_tool_context_message(
    context: AgentContext,
    message: Message,
    *,
    tool_name: str,
    tool_call_id: str,
) -> None:
    """Append provider-visible context injected by a completed tool call."""
    copied = message.model_copy(deep=True)
    metadata = message_entry_metadata(copied)
    metadata.update(
        {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
        }
    )
    branching = append_conversation_entry(
        context,
        ConversationEntry(
            kind="tool_context",
            message=copied,
            metadata=metadata,
        ),
    )
    update_message_projection(context, copied, branching=branching)


def append_skill_message(context: AgentContext, message: Message) -> None:
    """Append activated skill instructions to the context log."""
    copied = message.model_copy(deep=True)
    skill_name = skill_name_from_message(copied)
    existing_activation_ids = {
        activation_id
        for entry in context.conversation_log.entries
        if entry.kind == "skill_event"
        and isinstance(
            activation_id := entry.metadata.get("skill_activation_id"),
            str,
        )
    }
    activation_id = next(
        (
            skill.activation_id
            for skill in reversed(context.active_skills)
            if skill.name == skill_name
            and skill.activation_id
            and skill.activation_id not in existing_activation_ids
        ),
        None,
    )
    branching = append_conversation_entry(
        context,
        skill_message_conversation_entry(
            copied,
            parent_id=None,
            skill_name=skill_name,
            skill_activation_id=activation_id,
        ),
    )
    update_message_projection(context, copied, branching=branching)


def append_tool_result(
    context: AgentContext,
    *,
    tool_call_id: str,
    result: dict[str, object],
    provider_result_projection: str | None = None,
) -> Message:
    """Append a tool result message to the context and return it."""
    message = Message.tool(
        tool_call_id=tool_call_id,
        content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    )
    message._provider_result_projection = provider_result_projection
    metadata = message_entry_metadata(message)
    if provider_result_projection is not None:
        metadata["provider_result_projection"] = provider_result_projection
    branching = append_conversation_entry(
        context,
        ConversationEntry(
            kind="tool_result",
            message=message.model_copy(deep=True),
            metadata=metadata,
        ),
    )
    update_message_projection(context, message, branching=branching)
    return message


def transcript_messages(context: AgentContext) -> list[Message]:
    """Return the compact runtime transcript from canonical history."""
    messages = [message.model_copy(deep=True) for message in context.instructions]
    projection = project_conversation(
        context.conversation_log.entries,
        leaf_id=context.conversation_log.leaf_id,
    )
    messages.extend(
        message.model_copy(deep=True) for message in projection.runtime_messages
    )
    return messages


def newest_real_user_message(context: AgentContext) -> Message | None:
    """Return the newest user-authored message from the transcript."""
    for entry in reversed(context.conversation_log.entries):
        if entry.kind == "user" and entry.message is not None:
            return entry.message.model_copy(deep=True)
    return None


def message_image_count(message: Message) -> int:
    """Return the count of image inputs in one message."""
    if not isinstance(message.content, list):
        return 0
    return sum(
        1
        for part in message.content
        if isinstance(
            part,
            MessageImageURLContentPart | MessageLocalImageContentPart,
        )
    )
