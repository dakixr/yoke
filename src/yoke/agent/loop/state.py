"""State and context helpers for the agent loop."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


def context_for_run(
    agent,
    prompt: str,
    *,
    user_message: Message | None,
    available_skills: Sequence[SkillSpec] | None,
    active_skills: Sequence[ActiveSkill] | None,
) -> AgentContext:
    """Build the working context for one agent run."""
    resolved_available_skills = list(
        available_skills if available_skills is not None else agent.available_skills
    )
    resolved_active_skills = list(
        active_skills if active_skills is not None else agent.active_skills
    )
    if agent._context is None:
        return agent.context_manager.initialize(
            prompt,
            None,
            user_message=user_message,
            available_skills=resolved_available_skills,
            active_skills=resolved_active_skills,
        )
    context = agent._context.model_copy(deep=True)
    context.available_skills = [
        skill.model_copy(deep=True) for skill in resolved_available_skills
    ]
    context.active_skills = [
        skill.model_copy(deep=True) for skill in resolved_active_skills
    ]
    agent.context_manager.append_message(context, user_message or Message.user(prompt))
    return context


def persist_run_context(agent, context: AgentContext) -> None:
    """Persist the current run context back onto the agent."""
    agent._context = context
