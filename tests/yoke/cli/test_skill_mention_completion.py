"""Tests for interactive ``$skill`` completion."""

from dataclasses import dataclass

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from yoke.cli.interactive.completion import SlashCommandCompleter
from yoke.cli.interactive.completion import current_skill_mention_token


@dataclass
class _Skill:
    name: str
    description: str


def test_skill_mention_token_works_anywhere_in_multiline_prompt() -> None:
    assert current_skill_mention_token("Review this with $front") == "front"
    assert current_skill_mention_token("First line\n$review") == "review"
    assert current_skill_mention_token("$review continue") is None


def test_completer_replaces_only_the_current_mention_name() -> None:
    completer = SlashCommandCompleter(
        skills=[
            _Skill("frontend-design", "Design frontend UI."),
            _Skill("review", "Review code."),
        ]
    )

    completions = list(
        completer.get_completions(
            Document("Use $front", cursor_position=len("Use $front")),
            CompleteEvent(completion_requested=True),
        )
    )

    assert len(completions) == 1
    assert completions[0].text == "frontend-design"
    assert completions[0].start_position == -len("front")
    assert completions[0].display_text == "$frontend-design"
