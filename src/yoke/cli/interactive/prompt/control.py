"""Prompt-toolkit turn control helpers."""

from __future__ import annotations

from yoke.cli.backend.daemon import DaemonSessionBackend

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from io import StringIO
from threading import Event, Lock, Thread
from typing import Literal

from yoke.agent.models import Message
from yoke.cli.interactive.command_host import CommandCallbacks
from yoke.cli.interactive.commands import handle_slash_command
from yoke.cli.interactive.common import (
    PendingPrompt,
    PromptCliState,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
    session_resume_notice,
    session_busy,
)
from yoke.cli.interactive.prompt.cancellation import (
    retire_active_turn,
)
from yoke.cli.interactive.prompt.compaction import (
    start_prompt_compaction,
)
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.prompt.turns import (
    emit_turn_summary,
    finish_prompt_turn,
    handle_prompt_turn_outcome,
    run_prompt_turn,
)
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.skill_commands import is_skill_command
from yoke.cli.interactive.turn_renderer import (
    make_turn_scoped_renderer_factory,
)
from yoke.cli.render import build_console
from yoke.cli.backend.daemon import DaemonActiveSession
from yoke.cli.backend.values import ActiveSessionRef


@dataclass(slots=True)
class PromptToolkitControl:
    """Callbacks for prompt-toolkit session control."""

    start_turn: Callable[[str, Message | None], Thread]
    start_pending_prompt: Callable[[PendingPrompt | None, bool], None]
    resume_queue: Callable[[], None]
    start_compaction: Callable[[], Thread]
    request_exit: Callable[[], None]
    stop_active_turn: Callable[[], bool]
    steer_active_turn: Callable[[str, Message | None], bool]


def create_prompt_toolkit_control(  # noqa: C901
    *,
    state: PromptCliState,
    agent: DaemonSessionBackend,
    active_session_ref: ActiveSessionRef,
    renderer: PromptToolkitLiveRenderer,
    state_lock: Lock,
    request_context_usage: Callable[[str], None],
    invalidate_prompt: Callable[[], None],
    update_status: Callable[[str], None],
    scrollback: ScrollbackSink,
    retire_tool_traces: Callable[[int], None] | None = None,
) -> PromptToolkitControl:
    """Build the prompt-toolkit control callbacks."""
    turn_renderer_factory = make_turn_scoped_renderer_factory(
        state=state,
        state_lock=state_lock,
        renderer=renderer,
    )
    retire_traces = retire_tool_traces or (lambda _turn_id: None)

    def request_exit() -> None:
        with state_lock:
            state.shutdown_requested = True
        stopped = stop_active_turn()
        with state_lock:
            outcome_finishing = state.worker is not None
        if stopped or not outcome_finishing:
            _emit_prompt_exit_notice(
                state=state,
                state_lock=state_lock,
                active_session=active_session_ref.value,
                scrollback=scrollback,
            )
        update_status("Stopping")

    def start_turn(
        prompt: str,
        user_message: Message | None = None,
        *,
        continuation: bool = False,
        delivery: Literal["queue", "steer"] = "queue",
    ) -> Thread:
        stop_event = Event()
        active_user_message = user_message or Message.user(prompt)
        with active_session_ref.value.save_lock:
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

        def submitted(input_id: str) -> None:
            with state_lock:
                state.rendering_inputs[turn_id] = input_id

        def finished(input_id: str) -> None:
            with state_lock:
                state.rendering_inputs.pop(turn_id, None)
                state.fenced_inputs.discard(input_id)
                state.abandoned_turn_ids.discard(turn_id)

        def run_turn() -> None:
            run_prompt_turn(
                turn_id=turn_id,
                prompt=prompt,
                agent=agent,
                user_message=active_user_message,
                handle_outcome=handle_outcome,
                turn_renderer_factory=turn_renderer_factory,
                delivery=delivery,
                on_submitted=submitted,
                on_finished=finished,
            )

        thread = Thread(target=run_turn, daemon=True)
        with state_lock:
            state.worker = thread
        thread.start()
        request_context_usage(prompt)
        scrollback.emit("user", prompt)
        return thread

    def start_pending_prompt(
        pending: PendingPrompt | None,
        should_finish: bool,
    ) -> None:
        def submit_skill_prompt(prompt: str) -> None:
            start_turn(prompt)

        while pending is not None and is_skill_command(pending.prompt):
            captured_output = StringIO()
            handled, updated_messages, updated_session = handle_slash_command(
                pending.prompt,
                agent=agent,
                active_session=active_session_ref.value,
                messages=state.messages,
                console=build_console(captured_output),
                host=CommandCallbacks(on_submit_prompt=submit_skill_prompt),
            )
            if captured := captured_output.getvalue():
                scrollback.emit("raw", captured)
            if not handled:
                break
            with state_lock:
                state.messages = updated_messages
                active_session_ref.value = updated_session
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
            _emit_prompt_exit_notice(
                state=state,
                state_lock=state_lock,
                active_session=active_session_ref.value,
                scrollback=scrollback,
            )
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
                active_session=active_session_ref.value,
                renderer=renderer,
                scrollback=scrollback,
            )
            is None
        ):
            return
        with state_lock:
            if turn_id != state.active_turn_id:
                return
        pending, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            active_session=active_session_ref.value,
            request_context_usage=request_context_usage,
        )
        start_pending_prompt(pending, should_finish)

    def stop_active_turn() -> bool:
        with state_lock:
            stop_event = state.active_stop_request
            if not session_busy(state) and not (state.view and state.view.running):
                return False
            view = state.view
            input_id = (
                (view.running.id if view.running else view.runtime.input_id)
                if view
                else None
            )
            if view is not None and input_id is None:
                return False
            turn_start = state.turn_start_time
            turn_tools = state.turn_tool_count
            turn_in_tok = state.turn_input_tokens
            turn_out_tok = state.turn_output_tokens
            if stop_event is not None and (
                view is None
                or state.rendering_inputs.get(state.active_turn_id) == input_id
            ):
                if input_id is not None:
                    state.fenced_inputs.add(input_id)
                retire_active_turn(
                    state=state,
                    active_session=active_session_ref.value,
                    stop_event=stop_event,
                    status_message="",
                    retire_tool_traces=retire_traces,
                )
        # Key handlers must return immediately even on a slow daemon.
        if view is None:
            agent.interrupt()
        else:
            Thread(
                target=lambda: agent.stop_input(input_id),
                daemon=True,
                name="yoke-cli-stop",
            ).start()

        scrollback.emit(
            "notice",
            "Stopped current turn. Send a correction to continue from here.",
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
            if not session_busy(state):
                return False
            view = state.view
            input_id = (
                (view.running.id if view.running else view.runtime.input_id)
                if view
                else None
            )
            stop_event = state.active_stop_request
            if stop_event is not None and (
                view is None
                or state.rendering_inputs.get(state.active_turn_id) == input_id
            ):
                if input_id is not None:
                    state.fenced_inputs.add(input_id)
                retire_active_turn(
                    state=state,
                    active_session=active_session_ref.value,
                    stop_event=stop_event,
                    status_message="Steering",
                    retire_tool_traces=retire_traces,
                )
        start_turn(prompt, user_message, continuation=True, delivery="steer")
        scrollback.emit("notice", "Model steered.")
        invalidate_prompt()
        return True

    return PromptToolkitControl(
        start_turn=start_turn,
        resume_queue=lambda: Thread(
            target=agent.resume_queue, daemon=True, name="yoke-cli-resume"
        ).start(),
        start_pending_prompt=start_pending_prompt,
        start_compaction=partial(
            start_prompt_compaction,
            state=state,
            state_lock=state_lock,
            agent=agent,
            active_session_ref=active_session_ref,
            scrollback=scrollback,
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
    state_lock: Lock,
    active_session: DaemonActiveSession,
    scrollback: ScrollbackSink,
) -> None:
    with state_lock:
        if state.exit_notice_emitted:
            return
        state.exit_notice_emitted = True
    scrollback.emit("notice", session_resume_notice(active_session.id))
