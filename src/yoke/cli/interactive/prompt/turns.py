"""Prompt-toolkit turn execution helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event
from threading import Lock

from yoke.agent.loop import AgentStoppedError
from yoke.agent.loop.agent import RuntimeAgent
from yoke.session.workspace import promote_session_turn as promote_runtime_fork
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import capture_agent_state
from yoke.cli.config import RUN_ERRORS
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
from yoke.cli.interactive.queue.mutations import dequeue_prompt
from yoke.cli.interactive.queue.mutations import DequeuedPrompt, restore_dequeued_prompt
from yoke.cli.interactive.queue.mutations import (
    next_pending_prompt_index as next_pending_prompt_index,
)
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.runtime import ActiveSession, AgentRunner, EventRenderer
from yoke.cli.runtime import execute_turn
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import session_usage_metric_context
from yoke.cli.runtime import start_session_title_generation
from yoke.cli.interactive.prompt.scrollback import ScrollbackSink
from yoke.cli.interactive.prompt.runtime import prepare_turn_agent as prepare_turn_agent
from yoke.cli.interactive.prompt.runtime import retire_turn_agent as retire_turn_agent
from yoke.session.workspace import WorkspaceError, WorkspaceUnavailable
from yoke.session.workspace import require_workspace


def run_prompt_turn(
    *,
    turn_id: int,
    prompt: str,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session: ActiveSession,
    stop_event: Event,
    user_message: Message | None,
    callbacks: dict[str, Callable[..., object]],
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
    execution_started = False
    messages = (
        message_snapshot if message_snapshot is not None else list(state.messages)
    )
    entries = conversation_entries_snapshot
    if entries is None:
        entries = active_session.active_entries()
    title_messages = [*messages, user_message or Message.user(prompt)]
    turn_agent = agent

    def checkpoint_tool_result(
        checkpoint_messages: list[Message],
        checkpoint_entries: list[ConversationEntry],
    ) -> None:
        with active_session.save_lock:
            with state_lock:
                if turn_id != state.active_turn_id or stop_event.is_set():
                    return
                if isinstance(agent, RuntimeAgent) and isinstance(
                    turn_agent, RuntimeAgent
                ):
                    agent.active_skills = [
                        skill.model_copy(deep=True)
                        for skill in turn_agent.active_skills
                    ]
                state.messages = list(checkpoint_messages)
                # The checkpoint already contains the active user message.
                state.active_user_message = None
            persist_session_state(
                active_session,
                turn_agent,
                checkpoint_messages,
                conversation_entries=checkpoint_entries,
            )

    try:
        require_workspace(active_session.root, session_id=active_session.id)
        turn_agent = prepare_turn_agent(
            agent,
            messages=messages,
            entries=entries or [],
            active_session=active_session,
        )
        start_session_title_generation(
            active_session,
            agent,
            prompt,
            messages=title_messages,
        )
        with session_usage_metric_context(active_session, prompt):
            require_workspace(active_session.root, session_id=active_session.id)
            execution_started = True
            result = execute_turn(
                turn_agent,
                prompt,
                messages,
                indicator=turn_renderer_factory(turn_id),
                stop_requested=stop_event.is_set,
                user_message=user_message,
                conversation_entries=entries,
                after_tool_result_appended=checkpoint_tool_result,
            )
        if result.status == "stopped":
            callbacks["handle_outcome"](
                turn_id, TurnStopped(result=result, agent=turn_agent)
            )
            return
    except AgentStoppedError:
        state_snapshot = capture_agent_state(turn_agent)
        callbacks["handle_outcome"](
            turn_id,
            TurnStopped(
                messages=state_snapshot.messages,
                conversation_entries=state_snapshot.conversation_entries,
                agent=turn_agent,
            ),
        )
        return
    except (*RUN_ERRORS, WorkspaceError) as exc:
        callbacks["handle_outcome"](
            turn_id,
            TurnFailure(
                error=exc,
                messages=partial_messages_from_error(exc),
                conversation_entries=partial_conversation_entries_from_error(exc),
                agent=turn_agent,
                rejected_prompt=(
                    starting_prompt
                    if isinstance(exc, WorkspaceUnavailable) and not execution_started
                    else None
                ),
            ),
        )
        return
    callbacks["handle_outcome"](turn_id, TurnSuccess(result=result, agent=turn_agent))


def handle_prompt_turn_outcome(
    *,
    turn_id: int,
    outcome: TurnSuccess | TurnFailure | TurnStopped,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session: ActiveSession,
    renderer: PromptToolkitLiveRenderer,
    scrollback: ScrollbackSink,
) -> bool | None:
    """Apply a completed turn outcome to prompt-toolkit session state."""
    with state_lock:
        abandoned_turn_ids, steered_turn_ids = prompt_turn_tracking(state)
        if turn_id != state.active_turn_id or turn_id in abandoned_turn_ids:
            abandoned_turn_ids.discard(turn_id)
            steered_turn_ids.discard(turn_id)
            retire_turn_agent(outcome.agent, primary_agent=agent)
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
    if (
        not rejected
        and isinstance(agent, RuntimeAgent)
        and isinstance(outcome_agent, RuntimeAgent)
    ):
        if outcome_agent is not agent:
            promote_runtime_fork(agent, outcome_agent)
            outcome_agent = agent
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
            persist_session_state(
                active_session,
                outcome_agent,
                outcome.messages,
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
        retire_turn_agent(outcome.agent, primary_agent=agent)
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
                outcome_agent,
                stopped_messages,
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
        retire_turn_agent(outcome.agent, primary_agent=agent)
        return was_steered
    with state_lock:
        state.messages = outcome.result.messages
    persist_session_state(
        active_session,
        outcome_agent,
        outcome.result.messages,
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
    retire_turn_agent(outcome.agent, primary_agent=agent)
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
