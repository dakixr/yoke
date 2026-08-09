"""Interactive /skill command helpers."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.cli.render.base import Console
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import persist_session_state


def is_skill_command(command: str) -> bool:
    """Return whether text targets the skill slash command."""
    normalized = command.strip().lower()
    return normalized == "/skill" or normalized.startswith("/skill ")


def handle_skill_load(
    command: str,
    agent: object,
    active_session: ActiveSession,
    messages: list[Message],
    console: Console,
    *,
    on_submit_prompt: Callable[[str], None] | None = None,
) -> None:
    """Activate a discovered skill from a slash command."""
    from yoke.agent.loop.agent import RuntimeAgent
    from yoke.cli.render import print_scrollback_notice

    parsed = parse_skill_command(command)
    skill_name = "" if parsed is None else parsed[0]
    if not skill_name:
        print_scrollback_notice(console, "Usage: /skill <name> [prompt]")
        return
    if not isinstance(agent, RuntimeAgent) or agent.skill_registry is None:
        print_scrollback_notice(console, "No skills are available in this session.")
        return
    from yoke.agent.skills.activation import activate_skills

    activation = activate_skills(
        registry=agent.skill_registry,
        active_skills=agent.active_skills,
        names=[skill_name],
    )
    if activation.missing:
        print_scrollback_notice(console, f"Unknown skill: {skill_name}")
        return
    agent.active_skills = activation.active_skills
    tree = SessionTree.import_legacy(
        active_session.record.conversation_entries,
        active_session.record.leaf_id,
    )
    tree.append_active_skills(activation.activated_skills)
    exported = tree.export_for_persistence()
    active = tree.export_active_for_persistence()
    agent.load_conversation(
        conversation_entries=list(active.entries),
        active_skills=activation.active_skills,
    )
    persist_session_state(
        active_session,
        agent,
        messages,
        conversation_entries=list(exported.entries),
    )
    initial_prompt = parsed[1] if parsed is not None else ""
    print_scrollback_notice(console, f"Activated skill: {skill_name}")
    if initial_prompt and on_submit_prompt is not None:
        on_submit_prompt(initial_prompt)


def parse_skill_command(command: str) -> tuple[str, str] | None:
    """Return the skill name and optional prompt tail for a /skill command."""
    stripped = command.strip()
    if stripped.lower() == "/skill":
        return None
    prefix = "/skill "
    if not stripped.lower().startswith(prefix):
        return None
    remainder = stripped[len(prefix) :].strip()
    if not remainder:
        return None
    skill_name, separator, prompt = remainder.partition(" ")
    return skill_name, prompt.strip() if separator else ""
