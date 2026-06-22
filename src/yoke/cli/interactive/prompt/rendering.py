"""Prompt-toolkit rendering helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Thread
from typing import cast

from rich.text import Text

from yoke.cli.image_input import format_attachment_lines
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import SHORTCUT_LINES
from yoke.cli.interactive.renderer import format_bottom_toolbar
from yoke.cli.render import print_version_banner
from yoke.cli.render import print_session_scrollback


def run_scrollback_render(
    *,
    loop: object,
    render: Callable[[], None],
    run_in_terminal: Callable[[Callable[[], None]], object],
) -> None:
    """Render scrollback through prompt-toolkit only on a live asyncio loop."""
    if not isinstance(loop, asyncio.AbstractEventLoop):
        render()
        return
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        render()
        return
    if current_loop is not loop:
        render()
        return
    run_in_terminal(render)


def initialize_prompt_toolkit_session(
    *,
    state: PromptCliState,
    replay_session: bool,
    scrollback_console,
    start_turn: Callable[..., Thread],
) -> None:
    """Print intro text and seed the session when needed."""
    print_version_banner(scrollback_console)
    scrollback_console.print(
        Text(
            "\n".join(SHORTCUT_LINES) + "\n",
            style="dim",
        )
    )
    if replay_session and state.messages:
        print_session_scrollback(scrollback_console, state.messages)
    if replay_session or not state.messages or state.messages[-1].role != "user":
        return
    seeded_message = state.messages[-1].model_copy(deep=True)
    seeded_prompt = seeded_message.display_text_content() or ""
    state.messages = state.messages[:-1]
    if seeded_prompt:
        start_turn(seeded_prompt, user_message=seeded_message)


def build_prompt_toolbar(
    *,
    state: PromptCliState,
    state_lock,
    provider_model_text: Callable[[], str | None] | str | None,
    spinner_frames: tuple[str, ...],
    root_label: str,
    session_title_text: Callable[[], str | None] | str | None = None,
) -> Callable[[], list[tuple[str, str]]]:
    """Create the toolbar callback used by prompt-toolkit."""

    def get_bottom_toolbar() -> list[tuple[str, str]]:
        with state_lock:
            stop_pending = bool(
                state.active_stop_request and state.active_stop_request.is_set()
            )
            current_worker = state.worker
            queued_prompts = list(state.pending_prompts)
            pending_images = list(state.pending_images)
            current_status = state.status_message
            current_context_usage = state.context_usage_text
            current_usage_percent = state.context_usage_percent
            current_input_tokens = state.context_input_tokens
            current_max_tokens = state.context_max_tokens
            turn_start = state.turn_start_time
            turn_tools = state.turn_tool_count
            turn_in_tok = state.turn_input_tokens
            turn_out_tok = state.turn_output_tokens
            turn_reason_tok = state.turn_reasoning_tokens
            turn_num = state.active_turn_id
        frame = None
        if current_worker is not None and not stop_pending:
            frame = spinner_frames[state.spinner_index % len(spinner_frames)]
            state.spinner_index += 1
        current_provider_model_text: str | None
        if callable(provider_model_text):
            provider_status_getter = cast(Callable[[], str | None], provider_model_text)
            current_provider_model_text = provider_status_getter()
        else:
            current_provider_model_text = provider_model_text
        current_session_title_text: str | None
        if callable(session_title_text):
            session_title_getter = cast(Callable[[], str | None], session_title_text)
            current_session_title_text = session_title_getter()
        else:
            current_session_title_text = session_title_text
        elapsed = None
        if turn_start is not None and current_worker is not None and not stop_pending:
            import time

            elapsed = time.monotonic() - turn_start
        return format_bottom_toolbar(
            worker_active=current_worker is not None,
            stop_pending=stop_pending,
            status_message=current_status,
            pending_prompts=_copy_pending_prompts(queued_prompts),
            pending_images=format_attachment_lines(pending_images),
            context_usage=current_context_usage,
            context_usage_percent=current_usage_percent,
            context_input_tokens=current_input_tokens,
            context_max_tokens=current_max_tokens,
            provider_model=current_provider_model_text,
            root_label=root_label,
            session_title=current_session_title_text,
            spinner_frame=frame,
            columns=_current_output_columns(),
            turn_elapsed_seconds=elapsed,
            turn_tool_count=turn_tools,
            turn_input_tokens=turn_in_tok,
            turn_output_tokens=turn_out_tok,
            turn_reasoning_tokens=turn_reason_tok,
            turn_number=turn_num,
        )

    return get_bottom_toolbar


def _current_output_columns() -> int | None:
    try:
        from prompt_toolkit.application.current import get_app_or_none
    except ImportError:
        return None
    app = get_app_or_none()
    if app is None:
        return None
    return app.output.get_size().columns


def _copy_pending_prompts(
    prompts: list[PendingPrompt],
) -> list[PendingPrompt]:
    """Return a shallow copy with the declared prompt type."""
    return list(prompts)
