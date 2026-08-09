"""Tests for prompt-toolkit session rendering."""

from __future__ import annotations

import io
from threading import Thread

from yoke import __version__
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.rendering import initialize_prompt_toolkit_session
from yoke.cli.render import build_console


def test_interactive_startup_prints_only_version_banner() -> None:
    stream = io.StringIO()

    def unexpected_start_turn(*_args: object, **_kwargs: object) -> Thread:
        raise AssertionError("An empty session must not seed a turn.")

    initialize_prompt_toolkit_session(
        state=PromptCliState(messages=[], pending_prompts=[]),
        replay_session=False,
        replay_messages=None,
        replay_notice=None,
        scrollback_console=build_console(stream),
        start_turn=unexpected_start_turn,
    )

    assert stream.getvalue() == f"Version {__version__}\n"
