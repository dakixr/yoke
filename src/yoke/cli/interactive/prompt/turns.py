"""Prompt-toolkit turn execution helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event, Lock

from yoke.cli.backend import SessionBackend
from yoke.cli.interactive.turn_stream import render_turn_events
from yoke.agent.models import ConversationEntry, Message
from yoke.cli.interactive.common import (
    PendingPrompt,
    PromptCliState,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.queue.mutations import (
    DequeuedPrompt,
    dequeue_prompt,
    restore_dequeued_prompt,
)
from yoke.cli.interactive.queue.mutations import (
    next_pending_prompt_index as next_pending_prompt_index,
)
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.runtime import (
    ActiveSession,
    EventRenderer,
)


def run_prompt_turn(
    *,
    turn_id: int,
    prompt: str,
    state: PromptCliState,
    state_lock: Lock,
    agent: object,
    active_session: ActiveSession,
    stop_event: Event,
    user_message: Message | None,
    handle_outcome: Callable[[int, TurnSuccess | TurnFailure | TurnStopped], None],
    turn_renderer_factory: Callable[[int], EventRenderer],
    message_snapshot: list[Message] | None = None,
    conversation_entries_snapshot: list[ConversationEntry] | None = None,
    starting_prompt: tuple[PendingPrompt, int] | None = None,
) -> None:
    """Execute one prompt-toolkit turn in a worker thread."""
    with state_lock:
        if starting_prompt is None:
            starting_prompt = state.starting_prompt
            state.starting_prompt = None
    if starting_prompt is None:
        starting_prompt = (PendingPrompt(prompt, user_message=user_message), 0)
    messages = (
        message_snapshot if message_snapshot is not None else list(state.messages)
    )
    entries = conversation_entries_snapshot
    if entries is None:
        entries = active_session.active_entries()
    backend = SessionBackend.wrap(agent, active_session)

    def checkpoint_tool_result(
        turn_agent: object,
        checkpoint_messages: list[Message],
        checkpoint_entries: list[ConversationEntry],
    ) -> None:
        with active_session.save_lock:
            with state_lock:
                if turn_id != state.active_turn_id or stop_event.is_set():
                    return
                backend.checkpoint_skills(turn_agent)
                state.messages = list(checkpoint_messages)
                # The checkpoint already contains the active user message.
                state.active_user_message = None
            SessionBackend.wrap(agent, active_session).persist(
                checkpoint_messages,
                turn_agent=turn_agent,
                conversation_entries=checkpoint_entries,
            )

    submitted = backend.submit(
        prompt,
        messages,
        entries=entries,
        stop_event=stop_event,
        user_message=user_message,
        checkpoint=checkpoint_tool_result,
        starting_prompt=starting_prompt,
    )
    render_turn_events(submitted, turn_renderer_factory(turn_id))
    handle_outcome(turn_id, submitted.result())


def handle_prompt_turn_outcome(
    *,
    turn_id: int,
    outcome: TurnSuccess | TurnFailure | TurnStopped,
    state: PromptCliState,
    state_lock: Lock,
    agent: object,
    active_session: ActiveSession,
    renderer: PromptToolkitLiveRenderer,
    scrollback: ScrollbackSink,
) -> bool | None:
    """Apply a completed turn outcome to prompt-toolkit session state."""
    with state_lock:
        abandoned_turn_ids, steered_turn_ids = (
            state.abandoned_turn_ids,
            state.steered_turn_ids,
        )
        if turn_id != state.active_turn_id or turn_id in abandoned_turn_ids:
            abandoned_turn_ids.discard(turn_id)
            steered_turn_ids.discard(turn_id)
            SessionBackend.wrap(agent, active_session).retire_turn(outcome.agent)
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
    outcome_agent = outcome.agent or agent
    rejected = isinstance(outcome, TurnFailure) and outcome.rejected_prompt is not None
    backend = SessionBackend.wrap(agent, active_session)
    if not rejected:
        outcome_agent = backend.promote_turn(outcome_agent)
    if isinstance(outcome, TurnFailure):
        if outcome.rejected_prompt is not None:
            pending, index = outcome.rejected_prompt
            paused = pending.copy_for_queue()
            paused.paused = True
            restore_dequeued_prompt(
                state=state,
                state_lock=state_lock,
                active_session=active_session,
                dequeued=DequeuedPrompt(paused, index),
            )
            scrollback.emit("notice", "Input saved paused in /queue.")
        if outcome.messages is not None:
            with state_lock:
                state.messages = outcome.messages
            SessionBackend.wrap(agent, active_session).persist(
                outcome.messages,
                turn_agent=outcome_agent,
                conversation_entries=outcome.conversation_entries,
            )
        renderer.print_error(str(outcome.error))
        emit_turn_summary(
            renderer,
            turn_start=turn_start,
            tool_count=turn_tools,
            input_tokens=turn_in_tok,
            output_tokens=turn_out_tok,
        )
        SessionBackend.wrap(agent, active_session).retire_turn(outcome.agent)
        return was_steered
    if isinstance(outcome, TurnStopped):
        stopped_messages = (
            outcome.result.messages if outcome.result is not None else outcome.messages
        )
        stopped_entries = (
            outcome.result.conversation_entries
            if outcome.result is not None
            else outcome.conversation_entries
        )
        if stopped_messages is not None:
            with state_lock:
                state.messages = stopped_messages
            SessionBackend.wrap(agent, active_session).persist(
                stopped_messages,
                turn_agent=outcome_agent,
                conversation_entries=stopped_entries,
            )
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
        SessionBackend.wrap(agent, active_session).retire_turn(outcome.agent)
        return was_steered
    with state_lock:
        state.messages = outcome.result.messages
    SessionBackend.wrap(agent, active_session).persist(
        outcome.result.messages,
        turn_agent=outcome_agent,
        conversation_entries=outcome.result.conversation_entries,
    )
    renderer.print_agent_output(outcome.result.output)
    emit_turn_summary(
        renderer,
        turn_start=turn_start,
        tool_count=turn_tools,
        input_tokens=turn_in_tok,
        output_tokens=turn_out_tok,
    )
    scrollback.emit("bell")
    SessionBackend.wrap(agent, active_session).retire_turn(outcome.agent)
    return was_steered


def finish_prompt_turn(
    *,
    state: PromptCliState,
    state_lock: Lock,
    active_session: ActiveSession,
    request_context_usage: Callable[[str], None],
) -> tuple[PendingPrompt | None, bool]:
    """Clear active turn state and return next prompt/shutdown flags."""
    with state_lock:
        state.turn_handoff_active = True
        state.worker = None
        state.active_stop_request = None
        state.active_user_message = None
        state.starting_prompt = None
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
