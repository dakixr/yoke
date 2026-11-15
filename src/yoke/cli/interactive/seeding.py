"""Replay and initial user-message handling for both interactive front ends."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.models import Message
from yoke.cli.interactive.common import BasicCliState, PromptCliState
from yoke.cli.render import print_scrollback_notice, print_session_scrollback
from yoke.cli.render.base import Console


def seed_session(
    state: BasicCliState | PromptCliState,
    *,
    replay_session: bool,
    replay_messages: list[Message] | None,
    replay_notice: str | None,
    console: Console,
    start_turn: Callable[[str, Message], object],
) -> None:
    """Replay history or submit the trailing user message with its original blocks.

    Display text follows the transcript projection, currently equal to text_content.
    The original message carries every text and image block into provider delivery.
    """
    if replay_session and state.messages:
        if replay_notice:
            print_scrollback_notice(console, replay_notice)
        print_session_scrollback(console, replay_messages or state.messages)
    if replay_session or not state.messages or state.messages[-1].role != "user":
        return
    message = state.messages[-1].model_copy(deep=True)
    prompt = message.display_text_content() or ""
    state.messages = state.messages[:-1]
    if prompt:
        start_turn(prompt, message)
