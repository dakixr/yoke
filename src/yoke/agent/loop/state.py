"""State and context helpers for the agent loop."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.skills.context import (
    append_missing_active_skill_messages,
)
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.mentions import activate_mentioned_skills
from yoke.ai.providers.base import start_provider_turn


def context_for_run(
    agent,
    prompt: str,
    *,
    user_message: Message | None,
    available_skills: Sequence[SkillSpec] | None,
    active_skills: Sequence[ActiveSkill] | None,
    append_user_message: bool = True,
) -> AgentContext:
    """Build the working context for one agent run."""
    if not append_user_message and (prompt or user_message is not None):
        raise ValueError("Continuation must not contain user input")
    if not append_user_message and (
        agent._context is None
        or not any(message.role == "user" for message in agent._context.messages)
    ):
        raise ValueError("Continuation requires conversation history")
    start_provider_turn(agent.provider)
    resolved_available_skills = list(
        available_skills if available_skills is not None else agent.available_skills
    )
    resolved_active_skills = list(
        active_skills if active_skills is not None else agent.active_skills
    )
    if append_user_message:
        resolved_active_skills = activate_mentioned_skills(
            prompt=prompt,
            registry=agent.skill_registry,
            active_skills=resolved_active_skills,
        )
        agent.active_skills = [
            skill.model_copy(deep=True) for skill in resolved_active_skills
        ]
    if agent._context is None:
        context = agent.context_manager.initialize(
            prompt,
            None,
            user_message=user_message,
            append_prompt=False,
            available_skills=resolved_available_skills,
            active_skills=resolved_active_skills,
        )
        append_missing_active_skill_messages(context)
        agent.context_manager.append_message(
            context, user_message or Message.user(prompt)
        )
        return context
    if agent._context_owned_for_run:
        context = agent._context
        agent._context_owned_for_run = False
    else:
        context = agent._context.model_copy(deep=True)
    context.available_skills = [
        skill.model_copy(deep=True) for skill in resolved_available_skills
    ]
    context.active_skills = [
        skill.model_copy(deep=True) for skill in resolved_active_skills
    ]
    append_missing_active_skill_messages(context)
    if append_user_message:
        agent.context_manager.append_message(
            context, user_message or Message.user(prompt)
        )
    return context


def persist_run_context(agent, context: AgentContext) -> None:
    """Persist the current run context back onto the agent."""
    agent._context = context
