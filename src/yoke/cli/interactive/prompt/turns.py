"""Prompt-toolkit turn execution helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock
from typing import Literal

from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.interactive.turn_stream import render_turn_events
from yoke.agent.models import Message
from yoke.cli.interactive.common import (
    PendingPrompt,
    PromptCliState,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.queue.mutations import (
    dequeue_prompt,
)
from yoke.cli.interactive.queue.mutations import (
    next_pending_prompt_index as next_pending_prompt_index,
)
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.backend.daemon import DaemonActiveSession
from yoke.cli.backend.values import EventRenderer


def run_prompt_turn(
    *,
    turn_id: int,
    prompt: str,
    agent: DaemonSessionBackend,
    user_message: Message | None,
    handle_outcome: Callable[[int, TurnSuccess | TurnFailure | TurnStopped], None],
    turn_renderer_factory: Callable[[int], EventRenderer],
    delivery: Literal["queue", "steer"] = "queue",
    on_submitted: Callable[[str], None] = lambda _: None,
    on_finished: Callable[[str], None] = lambda _: None,
) -> None:
    """Execute one prompt-toolkit turn in a worker thread."""
    submitted = agent.submit(prompt, user_message=user_message, delivery=delivery)
    on_submitted(submitted.input_id)
    try:
        render_turn_events(submitted, turn_renderer_factory(turn_id))
        handle_outcome(turn_id, submitted.result())
    finally:
        on_finished(submitted.input_id)


def handle_prompt_turn_outcome(
    *,
    turn_id: int,
    outcome: TurnSuccess | TurnFailure | TurnStopped,
    state: PromptCliState,
    state_lock: Lock,
    agent: DaemonSessionBackend,
    active_session: DaemonActiveSession,
    renderer: PromptToolkitLiveRenderer,
    scrollback: ScrollbackSink,
) -> bool | None:
    """Apply a completed turn outcome to prompt-toolkit session state."""
    with state_lock:
        abandoned_turn_ids, steered_turn_ids = (
            state.abandoned_turn_ids,
            state.steered_turn_ids,
        )
        input_id = state.rendering_inputs.get(turn_id)
        fenced = (
            input_id in state.fenced_inputs
            if input_id
            else turn_id in abandoned_turn_ids
        )
        if fenced or (state.view is None and turn_id != state.active_turn_id):
            abandoned_turn_ids.discard(turn_id)
            steered_turn_ids.discard(turn_id)
            return None
        was_steered = turn_id in steered_turn_ids
        steered_turn_ids.discard(turn_id)
        # Claim the completed outcome while holding the same lock used by stop/steer.
        # A cancellation that won the race was handled above; one arriving after
        # this point must not turn an accepted result into mixed stopped/output UI.
        state.active_stop_request = None
        turn_start = state.turn_start_time
        turn_tools = state.turn_tool_count
        turn_in_tok = state.turn_input_tokens
        turn_out_tok = state.turn_output_tokens
    if isinstance(outcome, TurnFailure):
        if outcome.messages is not None:
            with state_lock:
                state.messages = outcome.messages
        renderer.print_error(str(outcome.error))
        emit_turn_summary(
            renderer,
            turn_start=turn_start,
            tool_count=turn_tools,
            input_tokens=turn_in_tok,
            output_tokens=turn_out_tok,
        )
        return was_steered
    if isinstance(outcome, TurnStopped):
        stopped_messages = (
            outcome.result.messages if outcome.result is not None else outcome.messages
        )
        if stopped_messages is not None:
            with state_lock:
                state.messages = stopped_messages
        scrollback.emit(
            "notice",
            "Model steered."
            if was_steered
            else "Stopped current turn. Send a correction to continue from here.",
        )
        emit_turn_summary(
            renderer,
            turn_start=turn_start,
            tool_count=turn_tools,
            input_tokens=turn_in_tok,
            output_tokens=turn_out_tok,
        )
        return was_steered
    with state_lock:
        state.messages = outcome.result.messages
    renderer.print_agent_output(outcome.result.output)
    emit_turn_summary(
        renderer,
        turn_start=turn_start,
        tool_count=turn_tools,
        input_tokens=turn_in_tok,
        output_tokens=turn_out_tok,
    )
    scrollback.emit("bell")
    return was_steered


def finish_prompt_turn(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: DaemonActiveSession,
    request_context_usage: Callable[[str], None],
) -> tuple[PendingPrompt | None, bool]:
    """Clear active turn state and return next prompt/shutdown flags."""
    with state_lock:
        state.turn_handoff_active = True
        state.worker = None
        state.active_stop_request = None
        state.active_user_message = None
        should_finish = state.shutdown_requested
    next_prompt = None
    if not should_finish:
        next_prompt = dequeue_prompt(
            state=state,
            state_lock=state_lock,
            active_session=active_session,
        )
    with state_lock:
        state.turn_handoff_active = next_prompt is not None
    request_context_usage("")
    return next_prompt, should_finish


def emit_turn_summary(
    renderer: PromptToolkitLiveRenderer,
    *,
    turn_start: float | None,
    tool_count: int,
    input_tokens: int | None,
    output_tokens: int | None,
    always: bool = False,
) -> None:
    """Emit the standard dim turn summary line."""
    emit = getattr(renderer, "_emit_turn_summary", None)
    if not callable(emit):
        return
    duration = None
    if turn_start is not None:
        duration = time.monotonic() - turn_start
    if duration is None or (duration < 60 and not always):
        return
    emit(
        {
            "duration_seconds": duration,
            "tool_count": tool_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )
