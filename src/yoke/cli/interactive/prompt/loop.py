"""Prompt-toolkit prompt loop helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock, Thread

from yoke.agent.models import Message
from yoke.cli.interactive.completion.menu import YokeCompletionsMenu
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.prompt.lifecycle import PromptLifecycleConfig
from yoke.cli.interactive.prompt.lifecycle import run_persistent_prompt_application
from yoke.cli.interactive.prompt.scrollback import BatchedScrollback
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
    state_lock: Lock,
    active_session: ActiveSession,
    agent: AgentRunner,
) -> None:
    """Persist prompt-toolkit state before leaving the interactive loop."""
    with state_lock:
        thinking_effort = state.thinking_effort
        pending_prompts = list(state.pending_prompts)
        pending_images = list(state.pending_images)
    persist_active_session_metadata(
        active_session,
        agent,
        reasoning_effort=thinking_effort,
    )
    persist_prompt_queue(
        active_session,
        pending_prompts,
        pending_images,
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
    submit_action: str | None = None,
) -> ActiveSession:
    """Process one submitted prompt-toolkit prompt."""
    active_session = active_session_ref["active_session"]
    with state_lock:
        action = state.submit_action if submit_action is None else submit_action
        if submit_action is None:
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

        def persist_current_queue() -> None:
            with state_lock:
                prompts = list(state.pending_prompts)
                images = list(state.pending_images)
                session = active_session_ref["active_session"]
            persist_prompt_queue(session, prompts, images)

        handled, updated_messages, updated_session = handle_slash_command(
            prompt,
            agent=agent,
            active_session=active_session,
            messages=state.messages,
            console=scrollback_console,
            pending_images=state.pending_images,
            pending_prompts=state.pending_prompts,
            on_queue_changed=persist_current_queue,
        )
        if handled:
            next_prompt_to_start: PendingPrompt | None = None
            steering_prompt_to_start: PendingPrompt | None = None
            queue_snapshot: tuple[list[PendingPrompt], list[ImageAttachment]] | None = (
                None
            )
            clear_queue = False
            with state_lock:
                state.messages = updated_messages
                active_session_ref["active_session"] = updated_session
                if not state.pending_prompts and not state.pending_images:
                    clear_queue = True
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
                            queue_snapshot = (
                                list(state.pending_prompts),
                                list(state.pending_images),
                            )
            if clear_queue:
                clear_prompt_queue(updated_session)
            if queue_snapshot is not None:
                persist_prompt_queue(
                    updated_session,
                    *queue_snapshot,
                )
            invalidate_prompt()
            if steering_prompt_to_start is not None:
                steer_active_turn(
                    steering_prompt_to_start.prompt,
                    steering_prompt_to_start.user_message,
                )
                persist_current_queue()
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
    scrollback: BatchedScrollback,
) -> int:
    """Run one persistent prompt-toolkit application."""
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

    def process_submission(prompt: str, action: str) -> None:
        process_prompt_toolkit_prompt(
            prompt,
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
            submit_action=action,
        )

    def persist_exit() -> None:
        with state_lock:
            active_session = active_session_ref["active_session"]
        persist_prompt_exit_state(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
            agent=agent,
        )

    return run_persistent_prompt_application(
        PromptLifecycleConfig(
            state=state,
            state_lock=state_lock,
            prompt_session=prompt_session,
            completer=completer,
            key_bindings=key_bindings,
            bottom_toolbar=get_bottom_toolbar,
            scrollback=scrollback,
            process_submission=process_submission,
            persist_exit=persist_exit,
            request_exit=request_exit,
        )
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
