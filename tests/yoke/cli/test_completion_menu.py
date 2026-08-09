"""Tests for prompt completion-menu behavior."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys

from yoke.cli.interactive.completion.menu import (
    register_completion_menu_key_bindings,
)


@dataclass
class _KeyEvent:
    current_buffer: Buffer
    arg: int = 1


def _handler_for(key: Keys) -> Callable[[KeyPressEvent], object]:
    bindings = KeyBindings()
    register_completion_menu_key_bindings(bindings)
    return next(
        binding.handler for binding in bindings.bindings if binding.keys == (key,)
    )


@pytest.mark.parametrize(
    ("key", "start", "expected"),
    (
        (Keys.Left, len("first\n"), len("first")),
        (Keys.Right, len("first"), len("first\n")),
    ),
)
def test_horizontal_arrows_cross_newline_boundaries(
    key: Keys,
    start: int,
    expected: int,
) -> None:
    """Horizontal navigation does not get trapped beside a newline."""
    buffer = Buffer()
    buffer.text = "first\nsecond"
    buffer.cursor_position = start
    legacy_offset = (
        buffer.document.get_cursor_left_position()
        if key == Keys.Left
        else buffer.document.get_cursor_right_position()
    )

    if legacy_offset != 0:
        raise AssertionError

    _handler_for(key)(cast(KeyPressEvent, _KeyEvent(buffer)))

    if buffer.cursor_position != expected:
        raise AssertionError


def test_horizontal_arrow_closes_completion_before_moving() -> None:
    """Horizontal editing restores original text and closes its menu."""
    buffer = Buffer()
    original = Document("/he", cursor_position=3)
    buffer.document = original
    buffer.complete_state = CompletionState(
        original,
        [Completion("/help", start_position=-3)],
        complete_index=0,
    )
    buffer.go_to_completion(0)

    _handler_for(Keys.Left)(cast(KeyPressEvent, _KeyEvent(buffer)))

    if buffer.text != "/he" or buffer.cursor_position != 2:
        raise AssertionError
    if buffer.complete_state is not None:
        raise AssertionError
