"""Interactive /skill command helpers."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.models import Message
from yoke.cli.backend import SessionBackend
from yoke.cli.render.base import Console
from yoke.cli.runtime import ActiveSession


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
    from yoke.cli.render import print_scrollback_notice

    parsed = parse_skill_command(command)
    skill_name = "" if parsed is None else parsed[0]
    if not skill_name:
        print_scrollback_notice(console, "Usage: /skill <name> [prompt]")
        return
    notice = SessionBackend.wrap(agent, active_session).activate_skill(
        skill_name, messages
    )
    if notice is not None:
        print_scrollback_notice(console, notice)
        return
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
