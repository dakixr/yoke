"""Prompt building, memory message rendering, and skill message utilities."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec

MEMORY_MESSAGE_PREFIX = (
    "Another language model started to solve this problem and produced a "
    "summary of its work.\n"
    "Use this summary to continue the task without redoing already completed "
    "investigation.\n"
    "Here is the summary:\n"
)
LEGACY_MEMORY_MESSAGE_PREFIX = (
    "Conversation memory summary:\n"
    "Auto-compacted summary of earlier conversation. This is lossy; "
    "rely on later messages when conflicts exist.\n"
)
CONTINUATION_NOTE = (
    "Continuation note: This task was compacted mid-turn. Continue working "
    "from the preserved recent tool calls and results. Do not treat "
    "compaction as task completion; keep making progress until the user's "
    "task is fully complete or you need clarification."
)
MEMORY_MESSAGE_SUFFIX = ""
LEGACY_MEMORY_MESSAGE_SUFFIX = (
    "\nUse this as historical context. Prioritize newer messages when conflicts exist."
)


class PromptContext(BaseModel):
    """Assembled prompt context with instructions, memory, and messages."""

    instructions: list[Message] = Field(default_factory=list)
    memory_messages: list[Message] = Field(default_factory=list)
    skill_messages: list[Message] = Field(default_factory=list)
    recent_messages: list[Message] = Field(default_factory=list)
    ordered_messages: list[Message] = Field(default_factory=list)


class PromptBuilder:
    """Assembles a PromptContext from an AgentContext for the provider."""

    def build(self, context: AgentContext) -> PromptContext:
        """Build a PromptContext from the given agent context."""
        instructions = [
            message.model_copy(deep=True) for message in context.instructions
        ]
        memory_messages: list[Message] = []
        skill_messages = (
            [
                render_available_skills_message(context.available_skills),
                *[
                    render_active_skill_message(skill)
                    for skill in context.active_skills
                ],
            ]
            if context.available_skills or context.active_skills
            else []
        )
        recent_messages: list[Message] = []
        ordered_messages: list[Message] = []
        visible_entries = _provider_visible_entries(context)
        if context.memory.current_snapshot is not None:
            memory_message = Message.user(
                render_memory_message(
                    context.memory.current_snapshot.summary_text,
                    continuation_note=bool(
                        context.memory.current_snapshot.metadata.get("mid_turn")
                    ),
                )
            )
            memory_messages.append(memory_message.model_copy(deep=True))
            for message in _retained_messages_from_snapshot(context):
                recent_messages.append(message.model_copy(deep=True))
                ordered_messages.append(message)
        for entry in visible_entries:
            if entry.kind == "instruction":
                continue
            if entry.kind == "memory_snapshot":
                continue
            if entry.message is not None:
                message = entry.message.model_copy(deep=True)
                recent_messages.append(message.model_copy(deep=True))
                ordered_messages.append(message)
        ordered_messages = [
            *[message.model_copy(deep=True) for message in memory_messages],
            *[message.model_copy(deep=True) for message in skill_messages],
            *ordered_messages,
        ]
        return PromptContext(
            instructions=instructions,
            memory_messages=memory_messages,
            skill_messages=skill_messages,
            recent_messages=recent_messages,
            ordered_messages=ordered_messages,
        )


def _provider_visible_entries(
    context: AgentContext,
) -> list[ConversationEntry]:
    last_memory_index: int | None = None
    for index, entry in enumerate(context.conversation_log.entries):
        if entry.kind == "memory_snapshot":
            last_memory_index = index
    if last_memory_index is None:
        return list(context.conversation_log.entries)
    return list(context.conversation_log.entries[last_memory_index + 1 :])


def _retained_messages_from_snapshot(context: AgentContext) -> list[Message]:
    snapshot = context.memory.current_snapshot
    if snapshot is None:
        return []
    if snapshot.compaction_handoff is not None:
        return [
            message.model_copy(deep=True)
            for message in snapshot.compaction_handoff.retained_messages
        ]
    raw_messages = snapshot.metadata.get("retained_messages")
    if not isinstance(raw_messages, list):
        return []
    messages: list[Message] = []
    for raw_message in raw_messages:
        try:
            messages.append(Message.model_validate(raw_message))
        except ValidationError:
            continue
    return messages


def render_memory_message(
    summary_text: str,
    *,
    continuation_note: bool = False,
) -> str:
    """Render a memory summary text into the memory message format."""
    parts = [f"{MEMORY_MESSAGE_PREFIX}{summary_text}"]
    if continuation_note:
        parts.append(CONTINUATION_NOTE)
    if MEMORY_MESSAGE_SUFFIX:
        parts.append(MEMORY_MESSAGE_SUFFIX)
    return "\n".join(parts)


def parse_memory_message(content: str) -> str | None:
    """Parse a memory message and return the summary text, or None."""
    if not content.startswith(MEMORY_MESSAGE_PREFIX):
        return _parse_legacy_memory_message(content)
    summary = (
        content.removeprefix(MEMORY_MESSAGE_PREFIX)
        .removesuffix(MEMORY_MESSAGE_SUFFIX)
        .rstrip()
    )
    if summary.endswith(CONTINUATION_NOTE):
        summary = summary[: -len(CONTINUATION_NOTE)].rstrip()
    return summary


def memory_message_has_continuation_note(content: str) -> bool:
    """Return True if the memory message contains a continuation note."""
    return (
        content.startswith(MEMORY_MESSAGE_PREFIX)
        or content.startswith(LEGACY_MEMORY_MESSAGE_PREFIX)
    ) and CONTINUATION_NOTE in content


def render_available_skills_message(skills: list[SkillSpec]) -> Message:
    """Render a system message listing all available skills."""
    lines = [
        "Available skills:",
        "Use the `skill` tool to load inactive skills by name when relevant, "
        "or to intentionally reload an active skill's canonical instructions.",
    ]
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description}")
    return Message.system("\n".join(lines))


def render_active_skill_message(skill: ActiveSkill) -> Message:
    """Render a system message for a currently loaded active skill."""
    lines = [
        "Active skill:",
        f"name: {skill.name}",
        f"description: {skill.description}",
        f"source: {skill.source_path}",
    ]
    if skill.file_paths:
        lines.extend(["files:"])
        lines.extend(f"- {path}" for path in skill.file_paths)
    if skill.reload_on_next_use:
        lines.extend(["", skill.load_content().strip()])
    else:
        lines.extend(
            [
                "",
                "Skill is active for this session. Reload its canonical "
                "instructions through the `skill` tool before relying on "
                "detailed workflow steps.",
            ]
        )
    return Message.system("\n".join(lines))


def _parse_legacy_memory_message(content: str) -> str | None:
    if not content.startswith(LEGACY_MEMORY_MESSAGE_PREFIX):
        return None
    if not content.endswith(LEGACY_MEMORY_MESSAGE_SUFFIX):
        return None
    summary = (
        content.removeprefix(LEGACY_MEMORY_MESSAGE_PREFIX)
        .removesuffix(LEGACY_MEMORY_MESSAGE_SUFFIX)
        .rstrip()
    )
    if summary.endswith(CONTINUATION_NOTE):
        summary = summary[: -len(CONTINUATION_NOTE)].rstrip()
    return summary
