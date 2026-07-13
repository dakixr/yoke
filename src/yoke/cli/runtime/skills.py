"""Skill-state restoration for resumed CLI sessions."""

from __future__ import annotations

from yoke.agent.loop import ConversationEntryHistory
from yoke.agent.loop import RuntimeAgent
from yoke.agent.state import active_branch_entries
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.session import save_active_session


def restore_active_session_skills(
    active_session: ActiveSession,
    agent: object,
) -> None:
    """Reconcile and persist durable skill state before resuming interaction."""
    if not isinstance(agent, RuntimeAgent):
        return
    record = active_session.record
    saved_active_skills = list(record.active_skills)
    agent.load_conversation(
        ConversationEntryHistory(
            active_branch_entries(
                record.conversation_entries,
                leaf_id=record.leaf_id,
            )
            or []
        ),
        active_skills=record.active_skills,
    )
    record.active_skills = [
        skill.model_copy(deep=True) for skill in agent.active_skills
    ]
    if record.active_skills != saved_active_skills:
        save_active_session(
            active_session,
            record.messages,
            conversation_entries=record.conversation_entries,
            leaf_id=record.leaf_id,
            agent=agent,
        )
