"""Prompt-toolkit prompt loop helpers."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from threading import Lock, Thread

from yoke.agent.models import Message
from yoke.cli.interactive.completion.menu import YokeCompletionsMenu
from yoke.cli.interactive.completion.menu import COMPLETION_MENU_STYLE
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive.prompt.submission import (
    submit_prompt_toolkit_prompt,
)
from yoke.cli.interactive.prompt.turns import next_pending_prompt_index
from yoke.cli.interactive.queue.persistence import clear_prompt_queue
from yoke.cli.interactive.queue.persistence import persist_prompt_queue
from yoke.cli.interactive.skill_commands import is_skill_command
from yoke.cli.render import print_session_scrollback
from yoke.cli.runtime import ActiveSession, AgentRunner
from yoke.cli.runtime.metadata import persist_active_session_metadata


def update_status_context_usage(
    payload: dict[str, object],
    *,
    state: PromptCliState,
    state_lock: Lock,
    invalidate_prompt: Callable[[], None],
    format_context_usage_text: Callable[[Mapping[str, object] | None], str | None],
) -> None:
    """Update prompt-toolkit context usage immediately from an event payload."""
    with state_lock:
        state.context_usage_text = format_context_usage_text(payload)
    invalidate_prompt()


def persist_prompt_exit_state(
    *,
    state: PromptCliState,
    active_session: ActiveSession,
    agent: AgentRunner,
) -> None:
    """Persist prompt-toolkit state before leaving the interactive loop."""
    persist_active_session_metadata(
        active_session,
        agent,
        reasoning_effort=state.thinking_effort,
    )
    persist_prompt_queue(
        active_session,
        list(state.pending_prompts),
        list(state.pending_images),
    )


def process_prompt_toolkit_prompt(  # noqa: C901
    prompt: str,
    *,
    state: PromptCliState,
    agent: AgentRunner,
    active_session_ref: dict[str, ActiveSession],
    scrollback_console,
    state_lock: Lock,
    update_status: Callable[[str], None],
    invalidate_prompt: Callable[[], None],
    request_exit: Callable[[], None],
    start_turn: Callable[..., Thread],
    start_pending_prompt: (Callable[[PendingPrompt | None, bool], None] | None) = None,
    start_compaction: Callable[[], Thread] | None = None,
    steer_active_turn: Callable[..., bool],
    open_process_inspector: Callable[[], None] | None = None,
    format_context_usage_text: Callable[[Mapping[str, object] | None], str | None],
    request_context_usage: Callable[[str], None] | None = None,
    on_editor_text: Callable[[str], None] | None = None,
) -> ActiveSession:
    """Process one submitted prompt-toolkit prompt."""
    active_session = active_session_ref["active_session"]
    action = state.submit_action
    state.submit_action = "steer"
    if not prompt and not state.pending_images:
        return active_session
    if action == "queue" and is_skill_command(prompt):
        with state_lock:
            must_wait = state.worker is not None or bool(state.pending_prompts)
        if must_wait:
            submit_prompt_toolkit_prompt(
                prompt,
                action=action,
                state=state,
                active_session=active_session,
                state_lock=state_lock,
                invalidate_prompt=invalidate_prompt,
                start_turn=start_turn,
                steer_active_turn=steer_active_turn,
            )
            return active_session
    if prompt.lower() in {"exit", "quit"}:
        request_exit()
        return active_session
    if prompt.strip().lower() == "/queue":
        handled, updated_messages, updated_session = handle_slash_command(
            prompt,
            agent=agent,
            active_session=active_session,
            messages=state.messages,
            console=scrollback_console,
            pending_images=state.pending_images,
            pending_prompts=state.pending_prompts,
            on_queue_changed=lambda: persist_prompt_queue(
                active_session_ref["active_session"],
                list(state.pending_prompts),
                list(state.pending_images),
            ),
        )
        if handled:
            next_prompt_to_start: PendingPrompt | None = None
            steering_prompt_to_start: PendingPrompt | None = None
            with state_lock:
                state.messages = updated_messages
                active_session_ref["active_session"] = updated_session
                if not state.pending_prompts and not state.pending_images:
                    clear_prompt_queue(updated_session)
                elif any(
                    pending.kind == "steering" and not pending.paused
                    for pending in state.pending_prompts
                ):
                    if (
                        state.worker is not None
                        and state.active_stop_request is not None
                    ):
                        next_index = next_pending_prompt_index(state.pending_prompts)
                        if next_index is not None:
                            steering_prompt_to_start = state.pending_prompts.pop(
                                next_index
                            )
                    else:
                        next_index = next_pending_prompt_index(state.pending_prompts)
                        if next_index is not None:
                            next_prompt_to_start = state.pending_prompts.pop(next_index)
                            persist_prompt_queue(
                                updated_session,
                                state.pending_prompts,
                                state.pending_images,
                            )
            invalidate_prompt()
            if steering_prompt_to_start is not None:
                steer_active_turn(
                    steering_prompt_to_start.prompt,
                    steering_prompt_to_start.user_message,
                )
                persist_prompt_queue(
                    updated_session,
                    state.pending_prompts,
                    state.pending_images,
                )
            elif next_prompt_to_start is not None:
                if start_pending_prompt is None:
                    start_turn(
                        next_prompt_to_start.prompt,
                        next_prompt_to_start.user_message,
                    )
                else:
                    start_pending_prompt(next_prompt_to_start, False)
            return updated_session
    if prompt.strip().lower() == "/compact" and start_compaction is not None:
        with state_lock:
            idle = state.worker is None and not state.pending_prompts
        if idle:
            start_compaction()
            if request_context_usage is not None:
                request_context_usage("")
            invalidate_prompt()
            return active_session_ref["active_session"]
    replay_messages_ref: list[list[Message] | None] = [None]
    handled, updated_messages, updated_session = handle_slash_command(
        prompt,
        agent=agent,
        active_session=active_session,
        messages=state.messages,
        console=scrollback_console,
        pending_images=state.pending_images,
        on_context_usage=lambda payload: update_status_context_usage(
            payload,
            state=state,
            state_lock=state_lock,
            invalidate_prompt=invalidate_prompt,
            format_context_usage_text=format_context_usage_text,
        ),
        on_editor_text=on_editor_text,
        on_submit_prompt=lambda submitted: submit_prompt_toolkit_prompt(
            submitted,
            action=action,
            state=state,
            active_session=active_session,
            state_lock=state_lock,
            invalidate_prompt=invalidate_prompt,
            start_turn=start_turn,
            steer_active_turn=steer_active_turn,
        ),
        on_replay_messages=lambda messages: replay_messages_ref.__setitem__(
            0,
            list(messages),
        ),
        on_process_inspector=open_process_inspector,
    )
    if handled:
        provider_config = getattr(getattr(agent, "provider", None), "config", None)
        provider_effort = getattr(provider_config, "reasoning_effort", None)
        with state_lock:
            state.messages = updated_messages
            active_session_ref["active_session"] = updated_session
            state.thinking_effort = (
                provider_effort
                if isinstance(provider_effort, str) and provider_effort.strip()
                else None
            )
            editor_text_for_usage = state.next_editor_text or ""
        if request_context_usage is not None:
            request_context_usage(editor_text_for_usage)
        replay_messages = replay_messages_ref[0]
        if replay_messages is not None:
            print_session_scrollback(scrollback_console, replay_messages)
        invalidate_prompt()
        return updated_session
    submit_prompt_toolkit_prompt(
        prompt,
        action=action,
        state=state,
        active_session=active_session,
        state_lock=state_lock,
        invalidate_prompt=invalidate_prompt,
        start_turn=start_turn,
        steer_active_turn=steer_active_turn,
    )
    return active_session_ref["active_session"]


def run_prompt_toolkit_event_loop(
    *,
    state: PromptCliState,
    active_session_ref: dict[str, ActiveSession],
    agent: AgentRunner,
    prompt_session,
    completer,
    key_bindings,
    state_lock: Lock,
    scrollback_console,
    provider_model_text: Callable[[], str | None] | str | None,
    session_title_text: Callable[[], str | None] | str | None,
    spinner_frames: tuple[str, ...],
    root_label: str,
    request_exit: Callable[[], None],
    update_status: Callable[[str], None],
    invalidate_prompt: Callable[[], None],
    start_turn: Callable[..., Thread],
    start_pending_prompt: Callable[[PendingPrompt | None, bool], None],
    start_compaction: Callable[[], Thread] | None = None,
    steer_active_turn: Callable[..., bool],
    open_process_inspector: Callable[[], None] | None = None,
    format_context_usage_text: Callable[[Mapping[str, object] | None], str | None],
    request_context_usage: Callable[[str], None],
) -> int:
    """Run the prompt-toolkit prompt loop."""
    from yoke.cli.interactive.prompt.rendering import (
        build_prompt_toolbar,
    )

    configure_prompt_session_completion_menu(prompt_session)
    get_bottom_toolbar = build_prompt_toolbar(
        state=state,
        state_lock=state_lock,
        provider_model_text=provider_model_text,
        session_title_text=session_title_text,
        spinner_frames=spinner_frames,
        root_label=root_label,
    )
    while True:
        with state_lock:
            if state.shutdown_requested and state.worker is None:
                active_session = active_session_ref["active_session"]
                persist_prompt_exit_state(
                    state=state,
                    active_session=active_session,
                    agent=agent,
                )
                return 0
        if state.shutdown_requested:
            time.sleep(0.05)
            continue
        try:
            with state_lock:
                default_text = state.next_editor_text or ""
                state.next_editor_text = None
            prompt = prompt_session.prompt(
                "› ",
                default=default_text,
                bottom_toolbar=get_bottom_toolbar,
                refresh_interval=0.2,
                key_bindings=key_bindings,
                completer=completer,
                complete_while_typing=True,
                multiline=True,
                reserve_space_for_menu=6,
                style=COMPLETION_MENU_STYLE,
            )
        except (EOFError, KeyboardInterrupt):
            request_exit()
            continue
        submitted_prompt = prompt
        if submitted_prompt.strip().lower() in {
            "exit",
            "quit",
            "/compact",
            "/shortcuts",
            "?",
            "/new",
            "/pin",
            "/info",
            "/fork",
            "/tree",
        }:
            submitted_prompt = submitted_prompt.strip()
        process_prompt_toolkit_prompt(
            submitted_prompt,
            state=state,
            agent=agent,
            active_session_ref=active_session_ref,
            scrollback_console=scrollback_console,
            state_lock=state_lock,
            update_status=update_status,
            invalidate_prompt=invalidate_prompt,
            request_exit=request_exit,
            start_turn=start_turn,
            start_pending_prompt=start_pending_prompt,
            start_compaction=start_compaction,
            steer_active_turn=steer_active_turn,
            open_process_inspector=open_process_inspector,
            format_context_usage_text=format_context_usage_text,
            request_context_usage=request_context_usage,
            on_editor_text=lambda text: setattr(
                state,
                "next_editor_text",
                text,
            ),
        )


def configure_prompt_session_completion_menu(prompt_session) -> None:
    """Replace prompt-toolkit's default popup with yoke's completion menu."""
    try:
        from prompt_toolkit.filters import has_focus
        from prompt_toolkit.layout.containers import Float

        default_buffer_window = prompt_session.layout.current_window
        default_buffer_window.content.menu_position = lambda: 0
        completion_filter = has_focus(prompt_session.default_buffer)
        prompt_wrapper = prompt_session.layout.container.children[0]
        float_container = prompt_wrapper.alternative_content
        floats = float_container.floats
        floats[:2] = [
            Float(
                xcursor=True,
                ycursor=True,
                transparent=True,
                content=YokeCompletionsMenu(
                    max_height=6,
                    extra_filter=completion_filter,
                ),
            )
        ]
        default_buffer_window.height = prompt_session._get_default_buffer_control_height
    except (AttributeError, IndexError, TypeError):
        return
