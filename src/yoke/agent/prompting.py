"""Prompt building, memory message rendering, and skill message utilities."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field

from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


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
        instructions = list(context.instructions)
        memory_messages: list[Message] = []
        skill_messages = (
            [
                render_available_skills_message(context.available_skills),
            ]
            if context.available_skills
            else []
        )
        recent_messages = list(context.messages[len(context.instructions) :])
        ordered_messages = [*memory_messages, *skill_messages, *recent_messages]
        return PromptContext(
            instructions=instructions,
            memory_messages=memory_messages,
            skill_messages=skill_messages,
            recent_messages=recent_messages,
            ordered_messages=ordered_messages,
        )


def render_available_skills_message(
    skills: list[SkillSpec],
) -> Message:
    """Render a system message listing all available skills."""
    lines = [
        "Available skills:",
        "Use the `skill` tool to activate skills by name when relevant.",
    ]
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description}")
    return Message.system("\n".join(lines))


def render_active_skill_message(skill: ActiveSkill) -> Message:
    """Render a system message for a currently loaded active skill."""
    directory_files = skill.directory_file_listing()
    lines = [
        "Active skill:",
        f"name: {skill.name}",
        f"description: {skill.description}",
        f"source: {skill.source_path}",
    ]
    if directory_files:
        lines.extend(["", "Skill directory files:"])
        lines.extend(f"- {path}" for path in directory_files)
    lines.extend(
        [
            "",
            skill.prepare_for_prompt().strip(),
        ]
    )
    return Message.system("\n".join(lines))
