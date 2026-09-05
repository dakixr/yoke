"""Tests for prompt-toolkit session rendering."""

from __future__ import annotations

import io
from collections.abc import Callable
from threading import Thread

import pytest
from rich.console import Console

from yoke import __version__
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.rendering import initialize_prompt_toolkit_session
from yoke.cli.render import build_console
from yoke.cli.render import format_user_prompt_block
from yoke.cli.render import print_scrollback_user
from yoke.cli.render import print_user_prompt


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


@pytest.mark.parametrize("encoding", ["utf-8", "cp437", "ascii"])
@pytest.mark.parametrize(
    ("printer", "plain_output"),
    [
        (print_user_prompt, "---\nuser:\nhello\n---\n"),
        (print_scrollback_user, "user hello\n"),
    ],
)
def test_user_prompt_rendering_respects_terminal_encoding(
    encoding: str,
    printer: Callable[[Console, str], None],
    plain_output: str,
) -> None:
    output = io.BytesIO()
    with io.TextIOWrapper(output, encoding=encoding, write_through=True) as stream:
        console = Console(
            file=stream, force_terminal=True, color_system=None, width=16, height=25
        )
        printer(console, "hello")
        rendered = output.getvalue().decode(encoding)

    expected = (
        plain_output
        if encoding == "ascii"
        else ("                \nhello           \n                \n")
    )
    assert rendered == expected


def test_user_prompt_block_preserves_blank_lines_and_wraps_to_terminal_width() -> None:
    console = Console(file=io.StringIO(), width=8)

    block = format_user_prompt_block(console, "one two\n\nthree four")

    assert block.plain == ("        \none two \n        \nthree   \nfour    \n        ")
