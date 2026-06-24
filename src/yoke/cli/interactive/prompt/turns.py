"""Prompt-toolkit turn execution helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event
from threading import Lock
from threading import Thread

from yoke.agent.loop import AgentStoppedError
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import capture_agent_state
from yoke.agent.state import active_branch_entries
from yoke.cli.config.runtime import RUN_ERRORS
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import (
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.common import (
    partial_conversation_entries_from_error,
)
from yoke.cli.interactive.common import partial_messages_from_error
from yoke.cli.interactive.common import prompt_turn_tracking
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.render import print_scrollback_notice
from yoke.cli.runtime import ActiveSession, AgentRunner, EventRenderer
from yoke.cli.runtime import execute_turn
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import start_session_title_generation


def run_prompt_turn(
    *,
    turn_id: int,
    prompt: str,
    state: PromptCliState,
    agent: AgentRunner,
    active_session: ActiveSession,
    stop_event: Event,
    user_message: Message | None,
    callbacks: dict[str, Callable[..., object]],
    turn_renderer_factory: Callable[[int], EventRenderer],
) -> None:
    """Execute one prompt-toolkit turn in a worker thread."""

    def checkpoint_tool_result(
        messages: list[Message],
        conversation_entries: list[ConversationEntry],
    ) -> None:
        persist_session_state(
            active_session,
            agent,
            messages,
            conversation_entries=conversation_entries,
        )

    try:
        result = execute_turn(
            agent,
            prompt,
            list(state.messages),
            indicator=turn_renderer_factory(turn_id),
            stop_requested=stop_event.is_set,
            user_message=user_message,
            conversation_entries=active_branch_entries(
                active_session.record.conversation_entries,
                leaf_id=active_session.record.leaf_id,
            ),
            after_tool_result_appended=checkpoint_tool_result,
        )
        if result.status == "stopped":
            callbacks["handle_outcome"](turn_id, TurnStopped(result=result))
            return
    except AgentStoppedError:
        state_snapshot = capture_agent_state(agent)
        callbacks["handle_outcome"](
            turn_id,
            TurnStopped(
                messages=state_snapshot.messages,
                conversation_entries=state_snapshot.conversation_entries,
            ),
        )
        return
    except RUN_ERRORS as exc:
        callbacks["handle_outcome"](
            turn_id,
            TurnFailure(
                error=exc,
                messages=partial_messages_from_error(exc),
                conversation_entries=partial_conversation_entries_from_error(exc),
            ),
        )
        return
    callbacks["handle_outcome"](turn_id, TurnSuccess(result=result))


def handle_prompt_turn_outcome(
    *,
    turn_id: int,
    outcome: TurnSuccess | TurnFailure | TurnStopped,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session: ActiveSession,
    renderer: PromptToolkitLiveRenderer,
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
    invalidate_prompt: Callable[[], None],
) -> bool | None:
    """Apply a completed turn outcome to prompt-toolkit session state."""
    with state_lock:
        abandoned_turn_ids, steered_turn_ids = prompt_turn_tracking(state)
        if turn_id in abandoned_turn_ids:
            abandoned_turn_ids.discard(turn_id)
            return None
        was_steered = turn_id in steered_turn_ids
        steered_turn_ids.discard(turn_id)
        turn_start = state.turn_start_time
        turn_tools = state.turn_tool_count
        turn_in_tok = state.turn_input_tokens
        turn_out_tok = state.turn_output_tokens
    if isinstance(outcome, TurnFailure):
        if outcome.messages is not None:
            with state_lock:
                state.messages = outcome.messages
            persist_session_state(
                active_session,
                agent,
                outcome.messages,
                conversation_entries=outcome.conversation_entries,
            )
        renderer.print_error(str(outcome.error))
        _emit_turn_summary(
            renderer,
            turn_id=turn_id,
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
        stopped_entries = (
            outcome.result.conversation_entries
            if outcome.result is not None
            else outcome.conversation_entries
        )
        if stopped_messages is not None:
            with state_lock:
                state.messages = stopped_messages
            persist_session_state(
                active_session,
                agent,
                stopped_messages,
                conversation_entries=stopped_entries,
            )
        run_in_scrollback(
            lambda: print_scrollback_notice(
                scrollback_console,
                "Model steered."
                if was_steered
                else ("Stopped current turn. Send a correction to continue from here."),
            )
        )
        _emit_turn_summary(
            renderer,
            turn_id=turn_id,
            turn_start=turn_start,
            tool_count=turn_tools,
            input_tokens=turn_in_tok,
            output_tokens=turn_out_tok,
        )
        return was_steered
    with state_lock:
        state.messages = outcome.result.messages
    persist_session_state(
        active_session,
        agent,
        outcome.result.messages,
        conversation_entries=outcome.result.conversation_entries,
    )
    start_session_title_generation(
        active_session,
        agent,
        outcome.result.messages,
        on_done=invalidate_prompt,
    )
    renderer.print_agent_output(outcome.result.output)
    _emit_turn_summary(
        renderer,
        turn_id=turn_id,
        turn_start=turn_start,
        tool_count=turn_tools,
        input_tokens=turn_in_tok,
        output_tokens=turn_out_tok,
    )
    print("\a", end="", flush=True)
    return was_steered


def finish_prompt_turn(
    *,
    state: PromptCliState,
    state_lock: Lock,
    estimate_toolbar_context_usage: Callable[[str], str | None],
) -> tuple[str | None, Message | None, bool]:
    """Clear active turn state and return next prompt/shutdown flags."""
    next_prompt: PendingPrompt | None = None
    should_finish = False
    with state_lock:
        state.worker = None
        state.active_stop_request = None
        state.active_user_message = None
        if any(not prompt.paused for prompt in state.pending_prompts):
            next_index = next_pending_prompt_index(state.pending_prompts)
            if next_index is not None:
                next_prompt = state.pending_prompts.pop(next_index)
        else:
            should_finish = state.shutdown_requested
    state.context_usage_text = estimate_toolbar_context_usage("")
    if next_prompt is None:
        return None, None, should_finish
    return next_prompt.prompt, next_prompt.user_message, should_finish


def next_pending_prompt_index(prompts: list[PendingPrompt]) -> int | None:
    """Return the next runnable prompt, prioritizing steering items."""
    for index, prompt in enumerate(prompts):
        if prompt.kind == "steering" and not prompt.paused:
            return index
    for index, prompt in enumerate(prompts):
        if not prompt.paused:
            return index
    return None


def get_active_turn_state(
    *,
    state: PromptCliState,
    state_lock: Lock,
) -> tuple[Event | None, Thread | None, int, Message | None]:
    """Return stop event, worker, and active turn id."""
    with state_lock:
        return (
            state.active_stop_request,
            state.worker,
            state.active_turn_id,
            state.active_user_message,
        )


def _emit_turn_summary(
    renderer: PromptToolkitLiveRenderer,
    *,
    turn_id: int,
    turn_start: float | None,
    tool_count: int,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    """Emit a dim 'Worked for ...' line only when the turn took over 60s."""
    emit = getattr(renderer, "_emit_turn_summary", None)
    if not callable(emit):
        return
    duration = None
    if turn_start is not None:
        duration = time.monotonic() - turn_start
    if duration is None or duration < 60:
        return
    emit(
        {
            "duration_seconds": duration,
            "tool_count": tool_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )
