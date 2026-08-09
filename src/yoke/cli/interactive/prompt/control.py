"""Prompt-toolkit turn control helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import Event
from threading import Lock
from threading import Thread

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import session_resume_notice
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive.common import (
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.prompt.cancellation import (
    persist_stopped_turn_if_idle,
)
from yoke.cli.interactive.prompt.cancellation import retire_active_turn
from yoke.cli.interactive.prompt.compaction import (
    start_prompt_compaction,
)
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.turn_renderer import (
    make_turn_scoped_renderer_factory,
)
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_scrollback_user
from yoke.cli.runtime import ActiveSession, AgentRunner
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.prompt.turns import emit_turn_summary
from yoke.cli.interactive.prompt.turns import handle_prompt_turn_outcome
from yoke.cli.interactive.prompt.turns import run_prompt_turn
from yoke.cli.interactive.skill_commands import is_skill_command


@dataclass(slots=True)
class PromptToolkitControl:
    """Callbacks for prompt-toolkit session control."""

    start_turn: Callable[[str, Message | None], Thread]
    start_pending_prompt: Callable[[PendingPrompt | None, bool], None]
    start_compaction: Callable[[], Thread]
    request_exit: Callable[[], None]
    stop_active_turn: Callable[[], bool]
    steer_active_turn: Callable[[str, Message | None], bool]


def create_prompt_toolkit_control(  # noqa: C901
    *,
    state: PromptCliState,
    agent: AgentRunner,
    active_session_ref: dict[str, ActiveSession],
    renderer: PromptToolkitLiveRenderer,
    scrollback_console,
    state_lock: Lock,
    request_context_usage: Callable[[str], None],
    invalidate_prompt: Callable[[], None],
    update_status: Callable[[str], None],
    run_in_scrollback: Callable[[Callable[[], None]], None],
    retire_tool_traces: Callable[[int], None] | None = None,
) -> PromptToolkitControl:
    """Build the prompt-toolkit control callbacks."""
    turn_renderer_factory = make_turn_scoped_renderer_factory(
        state=state,
        state_lock=state_lock,
        renderer=renderer,
    )
    retire_traces = retire_tool_traces or (lambda _turn_id: None)
    callbacks: dict[str, Callable[..., object]] = {}

    def request_exit() -> None:
        state.shutdown_requested = True
        stop_active_turn()
        _emit_prompt_exit_notice(
            state=state,
            active_session=active_session_ref["active_session"],
            scrollback_console=scrollback_console,
            run_in_scrollback=run_in_scrollback,
        )
        update_status("Stopping")

    def start_turn(
        prompt: str,
        user_message: Message | None = None,
        *,
        message_snapshot: list[Message] | None = None,
        conversation_entries_snapshot: list[ConversationEntry] | None = None,
        continuation: bool = False,
    ) -> Thread:
        stop_event = Event()
        active_user_message = user_message or Message.user(prompt)
        with state_lock:
            turn_messages = list(
                state.messages if message_snapshot is None else message_snapshot
            )
            turn_entries = (
                conversation_entries_snapshot
                if conversation_entries_snapshot is not None
                else state.continuation_entries
            )
            state.continuation_entries = None
        if turn_entries is None:
            turn_entries = active_session_ref["active_session"].active_entries()
        with state_lock:
            state.active_turn_id += 1
            turn_id = state.active_turn_id
            state.active_stop_request = stop_event
            state.active_user_message = active_user_message
            if not continuation or state.turn_start_time is None:
                state.turn_start_time = time.monotonic()
                state.turn_tool_count = 0
                state.turn_input_tokens = None
                state.turn_output_tokens = None
                state.turn_reasoning_tokens = None

        def run_turn() -> None:
            run_prompt_turn(
                turn_id=turn_id,
                prompt=prompt,
                state=state,
                state_lock=state_lock,
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
        request_context_usage(prompt)
        run_in_scrollback(lambda: print_scrollback_user(scrollback_console, prompt))
        return thread

    def start_pending_prompt(
        pending: PendingPrompt | None,
        should_finish: bool,
    ) -> None:
        def submit_skill_prompt(prompt: str) -> None:
            start_turn(prompt)

        while pending is not None and is_skill_command(pending.prompt):
            handled, updated_messages, updated_session = handle_slash_command(
                pending.prompt,
                agent=agent,
                active_session=active_session_ref["active_session"],
                messages=state.messages,
                console=scrollback_console,
                on_submit_prompt=submit_skill_prompt,
            )
            if not handled:
                break
            with state_lock:
                state.messages = updated_messages
                active_session_ref["active_session"] = updated_session
                turn_started = state.worker is not None
            if turn_started:
                return
            pending, should_finish = finish_prompt_turn(
                state=state,
                state_lock=state_lock,
                active_session=updated_session,
                request_context_usage=request_context_usage,
            )
        if pending is not None:
            start_turn(pending.prompt, pending.user_message)
            return
        if should_finish:
            invalidate_prompt()
            return
        update_status("")
        invalidate_prompt()

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
            )
            is None
        ):
            return
        pending, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            active_session=active_session_ref["active_session"],
            request_context_usage=request_context_usage,
        )
        start_pending_prompt(pending, should_finish)

    def stop_active_turn() -> bool:
        with state_lock:
            stop_event = state.active_stop_request
            current_worker = state.worker
            if current_worker is None or stop_event is None or stop_event.is_set():
                return False
            retired_turn_id = state.active_turn_id
            turn_start = state.turn_start_time
            turn_tools = state.turn_tool_count
            turn_in_tok = state.turn_input_tokens
            turn_out_tok = state.turn_output_tokens
            messages, entries = retire_active_turn(
                state=state,
                active_session=active_session_ref["active_session"],
                stop_event=stop_event,
                status_message="",
                retire_tool_traces=retire_traces,
            )

        Thread(
            target=persist_stopped_turn_if_idle,
            kwargs={
                "state": state,
                "retired_turn_id": retired_turn_id,
                "active_session": active_session_ref["active_session"],
                "agent": agent,
                "messages": messages,
                "entries": entries,
            },
            daemon=True,
            name="yoke-stop-checkpoint",
        ).start()
        run_in_scrollback(
            lambda: print_scrollback_notice(
                scrollback_console,
                "Stopped current turn. Send a correction to continue from here.",
            )
        )
        emit_turn_summary(
            renderer,
            turn_start=turn_start,
            tool_count=turn_tools,
            input_tokens=turn_in_tok,
            output_tokens=turn_out_tok,
            always=True,
        )
        invalidate_prompt()
        return True

    def steer_active_turn(prompt: str, user_message: Message | None = None) -> bool:
        with state_lock:
            stop_event = state.active_stop_request
            current_worker = state.worker
            if current_worker is None or stop_event is None or stop_event.is_set():
                return False
            messages, entries = retire_active_turn(
                state=state,
                active_session=active_session_ref["active_session"],
                stop_event=stop_event,
                status_message="Steering",
                retire_tool_traces=retire_traces,
            )
        start_turn(
            prompt,
            user_message,
            message_snapshot=messages,
            conversation_entries_snapshot=entries,
            continuation=True,
        )
        run_in_scrollback(
            lambda: print_scrollback_notice(scrollback_console, "Model steered.")
        )
        invalidate_prompt()
        return True

    callbacks["handle_outcome"] = handle_outcome
    return PromptToolkitControl(
        start_turn=start_turn,
        start_pending_prompt=start_pending_prompt,
        start_compaction=partial(
            start_prompt_compaction,
            state=state,
            state_lock=state_lock,
            agent=agent,
            active_session_ref=active_session_ref,
            scrollback_console=scrollback_console,
            run_in_scrollback=run_in_scrollback,
            request_context_usage=request_context_usage,
            update_status=update_status,
            start_pending_prompt=start_pending_prompt,
        ),
        request_exit=request_exit,
        stop_active_turn=stop_active_turn,
        steer_active_turn=steer_active_turn,
    )


def _emit_prompt_exit_notice(
    *,
    state: PromptCliState,
    active_session: ActiveSession,
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
) -> None:
    if state.exit_notice_emitted:
        return
    state.exit_notice_emitted = True
    run_in_scrollback(
        lambda: print_scrollback_notice(
            scrollback_console,
            session_resume_notice(active_session.id),
        )
    )
