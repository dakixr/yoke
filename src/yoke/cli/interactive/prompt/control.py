"""Prompt-toolkit turn control helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import Event
from threading import Lock
from threading import Thread

from yoke.agent.loop import INTERRUPTED_TURN_NOTICE
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import (
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.common import prompt_turn_tracking
from yoke.cli.interactive.prompt.compaction import (
    _persist_prompt_compaction as _persist_prompt_compaction,
)
from yoke.cli.interactive.prompt.compaction import start_prompt_compaction
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.turn_renderer import (
    make_turn_scoped_renderer_factory,
)
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_scrollback_user
from yoke.cli.runtime import ActiveSession, AgentRunner
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import resume_command_for_session_id
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.prompt.turns import handle_prompt_turn_outcome
from yoke.cli.interactive.prompt.turns import run_prompt_turn
from yoke.cli.interactive.queue.persistence import persist_prompt_queue


@dataclass(slots=True)
class PromptToolkitControl:
    """Callbacks for prompt-toolkit session control."""

    start_turn: Callable[[str, Message | None], Thread]
    start_compaction: Callable[[], Thread]
    request_exit: Callable[[], None]
    stop_active_turn: Callable[[], bool]
    steer_active_turn: Callable[[str, Message | None], bool]


def create_prompt_toolkit_control(
    *,
    state: PromptCliState,
    agent: AgentRunner,
    active_session_ref: dict[str, ActiveSession],
    renderer: PromptToolkitLiveRenderer,
    scrollback_console,
    state_lock: Lock,
    estimate_toolbar_context_usage: Callable[[str], str | None],
    invalidate_prompt: Callable[[], None],
    update_status: Callable[[str], None],
    run_in_scrollback: Callable[[Callable[[], None]], None],
) -> PromptToolkitControl:
    """Build the prompt-toolkit control callbacks."""
    turn_renderer_factory = make_turn_scoped_renderer_factory(
        state=state,
        state_lock=state_lock,
        renderer=renderer,
    )
    callbacks: dict[str, Callable[..., object]] = {}

    def request_exit() -> None:
        state.shutdown_requested = True
        if state.active_stop_request is not None:
            state.active_stop_request.set()
        emit_prompt_exit_notice(
            state=state,
            active_session=active_session_ref["active_session"],
            scrollback_console=scrollback_console,
            run_in_scrollback=run_in_scrollback,
        )
        if prompt_has_pending_work(state, state_lock):
            update_status("Finishing queued work before exit")

    def start_turn(
        prompt: str,
        user_message: Message | None = None,
        *,
        message_snapshot: list[Message] | None = None,
        conversation_entries_snapshot: list[ConversationEntry] | None = None,
    ) -> Thread:
        stop_event = Event()
        active_user_message = user_message or Message.user(prompt)
        with state_lock:
            turn_messages = list(
                state.messages if message_snapshot is None else message_snapshot
            )
        turn_entries = conversation_entries_snapshot
        if turn_entries is None:
            turn_entries = active_branch_entries(
                active_session_ref["active_session"].record.conversation_entries,
                leaf_id=active_session_ref["active_session"].record.leaf_id,
            )
        with state_lock:
            state.active_turn_id += 1
            turn_id = state.active_turn_id
            state.active_stop_request = stop_event
            state.active_user_message = active_user_message
            state.turn_start_time = time.monotonic()
            state.turn_tool_count = 0
            state.turn_input_tokens = None
            state.turn_output_tokens = None
            state.turn_reasoning_tokens = None
            state.status_message = ""

        def run_turn() -> None:
            run_prompt_turn(
                turn_id=turn_id,
                prompt=prompt,
                state=state,
                agent=agent,
                active_session=active_session_ref["active_session"],
                stop_event=stop_event,
                user_message=active_user_message,
                callbacks=callbacks,
                turn_renderer_factory=turn_renderer_factory,
                message_snapshot=turn_messages,
                conversation_entries_snapshot=turn_entries,
            )

        thread = Thread(target=run_turn, daemon=True)
        with state_lock:
            state.worker = thread
        thread.start()
        run_in_scrollback(lambda: print_scrollback_user(scrollback_console, prompt))
        context_usage_text = estimate_toolbar_context_usage(prompt)
        with state_lock:
            if state.active_turn_id == turn_id:
                state.context_usage_text = context_usage_text
        return thread

    def handle_outcome(
        turn_id: int,
        outcome: TurnSuccess | TurnFailure | TurnStopped,
    ) -> None:
        if (
            handle_prompt_turn_outcome(
                turn_id=turn_id,
                outcome=outcome,
                state=state,
                state_lock=state_lock,
                agent=agent,
                active_session=active_session_ref["active_session"],
                renderer=renderer,
                scrollback_console=scrollback_console,
                run_in_scrollback=run_in_scrollback,
                invalidate_prompt=invalidate_prompt,
            )
            is None
        ):
            return
        next_prompt, next_user_message, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            estimate_toolbar_context_usage=estimate_toolbar_context_usage,
        )
        if next_prompt is not None:
            persist_prompt_queue(
                active_session_ref["active_session"],
                state.pending_prompts,
                state.pending_images,
            )
            start_turn(next_prompt, next_user_message)
            return
        if should_finish:
            invalidate_prompt()
            return
        update_status("")

    def stop_active_turn() -> bool:
        with state_lock:
            stop_event = state.active_stop_request
            current_worker = state.worker
            if current_worker is None or stop_event is None or stop_event.is_set():
                return False
            abandoned_turn_ids, _ = prompt_turn_tracking(state)
            retired_turn_id = state.active_turn_id
            stop_event.set()
            abandoned_turn_ids.add(retired_turn_id)
            messages, entries = interrupted_turn_snapshot(
                messages=state.messages,
                entries=active_branch_entries(
                    active_session_ref["active_session"].record.conversation_entries,
                    leaf_id=active_session_ref["active_session"].record.leaf_id,
                )
                or [],
                user_message=state.active_user_message,
            )
            state.messages = messages
            state.worker = None
            state.active_stop_request = None
            state.active_user_message = None
            state.status_message = ""

        def persist_if_still_idle() -> None:
            with state_lock:
                if state.active_turn_id != retired_turn_id or state.worker is not None:
                    return
            persist_session_state(
                active_session_ref["active_session"],
                agent,
                messages,
                conversation_entries=entries,
            )

        Thread(
            target=persist_if_still_idle,
            daemon=True,
            name="yoke-stop-checkpoint",
        ).start()
        run_in_scrollback(
            lambda: print_scrollback_notice(
                scrollback_console,
                "Stopped current turn. Send a correction to continue from here.",
            )
        )
        invalidate_prompt()
        return True

    def steer_active_turn(prompt: str, user_message: Message | None = None) -> bool:
        with state_lock:
            stop_event = state.active_stop_request
            current_worker = state.worker
            if current_worker is None or stop_event is None:
                return False
            abandoned_turn_ids, _ = prompt_turn_tracking(state)
            retired_turn_id = state.active_turn_id
            stop_event.set()
            abandoned_turn_ids.add(retired_turn_id)
            messages, entries = interrupted_turn_snapshot(
                messages=state.messages,
                entries=active_branch_entries(
                    active_session_ref["active_session"].record.conversation_entries,
                    leaf_id=active_session_ref["active_session"].record.leaf_id,
                )
                or [],
                user_message=state.active_user_message,
            )
            state.messages = messages
            state.worker = None
            state.active_stop_request = None
            state.active_user_message = None
            state.status_message = "Steering"
        start_turn(
            prompt,
            user_message,
            message_snapshot=messages,
            conversation_entries_snapshot=entries,
        )
        run_in_scrollback(
            lambda: print_scrollback_notice(scrollback_console, "Model steered.")
        )
        invalidate_prompt()
        return True

    callbacks["handle_outcome"] = handle_outcome
    return PromptToolkitControl(
        start_turn=start_turn,
        start_compaction=partial(
            start_prompt_compaction,
            state=state,
            state_lock=state_lock,
            agent=agent,
            active_session_ref=active_session_ref,
            scrollback_console=scrollback_console,
            run_in_scrollback=run_in_scrollback,
            estimate_toolbar_context_usage=estimate_toolbar_context_usage,
            update_status=update_status,
            invalidate_prompt=invalidate_prompt,
            start_turn=start_turn,
        ),
        request_exit=request_exit,
        stop_active_turn=stop_active_turn,
        steer_active_turn=steer_active_turn,
    )


def interrupted_turn_snapshot(
    *,
    messages: list[Message],
    entries: list[ConversationEntry],
    user_message: Message | None,
) -> tuple[list[Message], list[ConversationEntry]]:
    """Build a durable continuation point without waiting for a retired turn."""
    snapshot_messages = list(messages)
    snapshot_entries = list(entries)
    parent_id = snapshot_entries[-1].id if snapshot_entries else None
    if user_message is not None:
        copied_user = user_message.model_copy(deep=True)
        snapshot_messages.append(copied_user)
        user_entry = ConversationEntry(
            kind="user",
            message=copied_user.model_copy(deep=True),
            parent_id=parent_id,
        )
        snapshot_entries.append(user_entry)
        parent_id = user_entry.id
    interrupted = Message.assistant(INTERRUPTED_TURN_NOTICE)
    snapshot_messages.append(interrupted)
    snapshot_entries.append(
        ConversationEntry(
            kind="assistant",
            message=interrupted.model_copy(deep=True),
            parent_id=parent_id,
        )
    )
    return snapshot_messages, snapshot_entries


def emit_prompt_exit_notice(
    *,
    state: PromptCliState,
    active_session: ActiveSession,
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
) -> None:
    """Emit the session resume notice once."""
    if state.exit_notice_emitted:
        return
    state.exit_notice_emitted = True
    run_in_scrollback(
        lambda: print_scrollback_notice(
            scrollback_console,
            f"To resume this session run:\n{resume_command_for_session_id(active_session.id)}",
        )
    )


def prompt_has_pending_work(state: PromptCliState, state_lock: Lock) -> bool:
    """Return whether there is active or queued prompt work."""
    with state_lock:
        return state.worker is not None or bool(state.pending_prompts)
