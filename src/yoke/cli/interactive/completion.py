"""Prompt completion helpers for the interactive CLI."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from typing import Protocol

from yoke.cli.interactive.common import SLASH_COMMANDS
from yoke.cli.interactive.common import SlashCommand
from prompt_toolkit.completion import Completer
from prompt_toolkit.completion import Completion
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document


class SkillLike(Protocol):
    """Skill metadata needed for prompt completion."""

    name: str
    description: str


class SlashCommandCompleter(Completer):
    """Complete slash commands at the start of an interactive prompt."""

    def __init__(
        self,
        commands: Iterable[SlashCommand] = SLASH_COMMANDS,
        skills: Iterable[SkillLike] = (),
        skill_provider: Callable[[], Iterable[SkillLike]] | None = None,
    ) -> None:
        self.commands = tuple(commands)
        self._skill_provider = skill_provider or (lambda: skills)

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterator[Completion]:
        """Yield prompt-toolkit completions for the current slash token."""
        del complete_event
        skill_token = current_slash_argument_token(
            document.text_before_cursor, "/skill"
        )
        if skill_token is not None:
            yield from self._skill_completions(
                skill_token, skills=self._skill_provider()
            )
            return
        token = current_slash_token(document.text_before_cursor)
        if token is None:
            return
        for command in self.commands:
            if not command.name.startswith(token):
                continue
            display = command.name
            if command.usage:
                display = f"{display} {command.usage}"
            yield Completion(
                command.name,
                start_position=-len(token),
                display=display,
                display_meta=command.description,
            )

    def _skill_completions(
        self,
        token: str,
        *,
        skills: Iterable[SkillLike],
    ) -> Iterator[Completion]:
        """Yield completions for `/skill <name>` arguments."""
        for skill in sorted(skills, key=lambda item: item.name):
            if not skill.name.startswith(token):
                continue
            yield Completion(
                skill.name,
                start_position=-len(token),
                display=skill.name,
                display_meta=skill.description,
            )


def current_slash_token(text_before_cursor: str) -> str | None:
    """Return the slash command token being edited, if any."""
    stripped = text_before_cursor.lstrip()
    if not stripped.startswith("/"):
        return None
    if any(char.isspace() for char in stripped):
        return None
    return stripped


def current_slash_argument_token(
    text_before_cursor: str,
    command: str,
) -> str | None:
    """Return the argument token being edited after a slash command."""
    stripped = text_before_cursor.lstrip()
    if stripped == command:
        return None
    prefix = f"{command} "
    if not stripped.startswith(prefix):
        return None
    token = stripped[len(prefix) :].lstrip()
    if any(char.isspace() for char in token):
        return None
    return token


def current_skill_name_token(text_before_cursor: str) -> str | None:
    """Return the skill name token being edited after `/skill`, if any."""
    return current_slash_argument_token(text_before_cursor, "/skill")
