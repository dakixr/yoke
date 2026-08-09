"""Helpers for appending skill instructions to conversation context."""

from __future__ import annotations

from yoke.agent.context.helpers import append_conversation_entry
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.prompting import render_active_skill_message
from yoke.agent.skills.models import ActiveSkill


def append_missing_active_skill_messages(context: AgentContext) -> None:
    """Append skill system messages for active skills not yet in context."""
    existing_ids = _skill_event_ids(context)
    existing_names = _skill_event_names(context)
    for skill in context.active_skills:
        activation_id = _skill_activation_id(skill)
        if activation_id in existing_ids:
            continue
        if skill.activation_id is None and skill.name in existing_names:
            continue
        entry = skill_conversation_entry(skill, parent_id=None)
        append_conversation_entry(context, entry)
        if entry.message is not None:
            context.messages.append(entry.message.model_copy(deep=True))
        existing_ids.add(activation_id)
        existing_names.add(skill.name)


def _skill_event_ids(context: AgentContext) -> set[str]:
    ids: set[str] = set()
    for entry in context.conversation_log.entries:
        if entry.kind != "skill_event":
            continue
        raw_id = entry.metadata.get("skill_activation_id")
        if isinstance(raw_id, str) and raw_id:
            ids.add(raw_id)
    return ids


def _skill_event_names(context: AgentContext) -> set[str]:
    return {
        name
        for entry in context.conversation_log.entries
        if entry.kind == "skill_event"
        and isinstance(name := entry.metadata.get("skill_name"), str)
        and name
    }


def skill_conversation_entry(
    skill: ActiveSkill,
    *,
    parent_id: str | None,
) -> ConversationEntry:
    """Build a conversation entry for one activated skill."""
    return skill_message_conversation_entry(
        render_active_skill_message(skill),
        parent_id=parent_id,
        skill_name=skill.name,
        skill_activation_id=_skill_activation_id(skill),
    )


def skill_message_conversation_entry(
    message: Message,
    *,
    parent_id: str | None,
    skill_name: str | None = None,
    skill_activation_id: str | None = None,
) -> ConversationEntry:
    """Build a conversation entry for an already rendered skill message."""
    metadata: dict[str, object] = {}
    resolved_name = skill_name or skill_name_from_message(message)
    if resolved_name:
        metadata["skill_name"] = resolved_name
    if skill_activation_id:
        metadata["skill_activation_id"] = skill_activation_id
    return ConversationEntry(
        kind="skill_event",
        message=message.model_copy(deep=True),
        parent_id=parent_id,
        metadata=metadata,
    )


def skill_name_from_message(message: Message) -> str | None:
    """Extract the rendered skill name from a skill system message."""
    content = message.plain_text_content or ""
    for line in content.splitlines():
        if line.startswith("name: "):
            return line.removeprefix("name: ").strip()
    return None


def _skill_activation_id(skill: ActiveSkill) -> str:
    return skill.activation_id or f"legacy:{skill.name}:{skill.source_path}"
