"""User and provider text must not be parsed as Rich markup."""

from __future__ import annotations

from collections.abc import Callable
import io

import pytest
from rich.console import Console

from yoke.cli.render import print_agent_output
from yoke.cli.render import print_error
from yoke.cli.render import print_scrollback_agent
from yoke.cli.render import print_scrollback_divider
from yoke.cli.render import print_scrollback_error
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_scrollback_tool
from yoke.cli.render import print_scrollback_user
from yoke.cli.render import print_scrollback_warning
from yoke.cli.render import print_user_prompt


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    ("printer", "prefix"),
    [
        (print_error, "Error: "),
        (print_scrollback_error, "error "),
        (print_scrollback_notice, "note "),
        (print_scrollback_warning, "warning "),
        (print_scrollback_tool, ""),
    ],
)
def test_diagnostic_text_is_literal(
    printer: Callable[[Console, str], None], prefix: str, terminal: bool
) -> None:
    stream = io.StringIO()
    console = Console(
        file=stream, force_terminal=terminal, color_system=None, width=200
    )
    message = "file [/missing] [red]literal[/red] :warning: [link=https://x.test]path"

    printer(console, message)

    assert stream.getvalue().splitlines()[-1] == prefix + message


@pytest.mark.parametrize(
    ("printer", "pattern"),
    [
        (print_agent_output, "{}\n"),
        (print_scrollback_agent, "\n{}\n\n"),
        (print_user_prompt, "---\nuser:\n{}\n---\n"),
        (print_scrollback_user, "user {}\n"),
    ],
)
def test_redirected_content_preserves_markup_and_emoji_aliases(
    printer: Callable[[Console, str], None],
    pattern: str,
) -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=200)
    message = "[red]literal[/red] [/missing] :snake: **Markdown**"

    printer(console, message)

    assert stream.getvalue() == pattern.format(message)


@pytest.mark.parametrize("terminal", [False, True])
def test_divider_label_is_literal(terminal: bool) -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=terminal, color_system=None, width=80)

    print_scrollback_divider(console, "[/missing] [red]literal[/red]", style="red")

    assert "[/missing] [red]literal[/red]" in stream.getvalue()
