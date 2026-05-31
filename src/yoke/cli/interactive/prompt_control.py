"""Prompt-toolkit turn control helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import Event
from threading import Lock
from threading import Thread

from yoke.agent.models import Message
from yoke.agent.loop.types import INTERRUPTED_TURN_NOTICE
from yoke.agent.state import active_branch_entries
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import (
    TurnFailure,
    TurnStopped,
    TurnSuccess,
)
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.common import prompt_turn_tracking
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.turn_renderer import (
    make_turn_scoped_renderer_factory,
)
from yoke.cli.render import format_compaction_note
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_scrollback_user
from yoke.cli.runtime import ActiveSession, AgentRunner
from yoke.cli.runtime import force_compact_history
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import save_active_session
from yoke.cli.interactive.prompt_turns import finish_prompt_turn
from yoke.cli.interactive.prompt_turns import get_active_turn_state
from yoke.cli.interactive.prompt_turns import handle_prompt_turn_outcome
from yoke.cli.interactive.prompt_turns import run_prompt_turn


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
        emit_prompt_exit_notice(
            state=state,
            active_session=active_session_ref["active_session"],
            scrollback_console=scrollback_console,
            run_in_scrollback=run_in_scrollback,
        )
        if prompt_has_pending_work(state, state_lock):
            update_status("Finishing queued work before exit")

    def start_turn(prompt: str, user_message: Message | None = None) -> Thread:
        stop_event = Event()
        active_user_message = user_message or Message.user(prompt)
        run_in_scrollback(lambda: print_scrollback_user(scrollback_console, prompt))
        with state_lock:
            state.active_turn_id += 1
            turn_id = state.active_turn_id
            state.active_stop_request = stop_event
            state.active_user_message = active_user_message
        state.context_usage_text = estimate_toolbar_context_usage(prompt)

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
            )

        thread = Thread(target=run_turn, daemon=True)
        with state_lock:
            state.worker = thread
        thread.start()
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
            start_turn(next_prompt, next_user_message)
            return
        if should_finish:
            invalidate_prompt()
            return
        update_status("")

    def stop_active_turn() -> bool:
        next_prompt: PendingPrompt | None = None
        stop_event, current_worker, turn_id, user_message = get_active_turn_state(
            state=state,
            state_lock=state_lock,
        )
        if current_worker is None or stop_event is None or stop_event.is_set():
            return False
        with state_lock:
            abandoned_turn_ids, _ = prompt_turn_tracking(state)
            stop_event.set()
            abandoned_turn_ids.add(turn_id)
            state.worker = None
            state.active_stop_request = None
            if state.pending_prompts:
                next_prompt = state.pending_prompts.pop(0)
            interrupted_messages = _interrupted_turn_messages(
                state.messages,
                user_message=user_message,
            )
            state.messages = interrupted_messages
            state.active_user_message = None
        save_active_session(
            active_session_ref["active_session"],
            interrupted_messages,
            agent=agent,
        )
        state.context_usage_text = estimate_toolbar_context_usage("")
        run_in_scrollback(
            lambda: print_scrollback_notice(
                scrollback_console,
                "Stopped current turn. Send a correction to continue from here.",
            )
        )
        if next_prompt is not None:
            start_turn(
                next_prompt.prompt,
                user_message=next_prompt.user_message,
            )
        else:
            update_status("")
        invalidate_prompt()
        return True

    def steer_active_turn(prompt: str, user_message: Message | None = None) -> bool:
        with state_lock:
            stop_event = state.active_stop_request
            current_worker = state.worker
            if current_worker is None or stop_event is None or stop_event.is_set():
                return False
            _, steered_turn_ids = prompt_turn_tracking(state)
            stop_event.set()
            steered_turn_ids.add(state.active_turn_id)
            state.pending_prompts.insert(
                0,
                PendingPrompt(
                    prompt,
                    user_message=user_message,
                    kind="steering",
                ),
            )
        update_status("Stopping current turn for steering")
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


def start_prompt_compaction(
    *,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session_ref: dict[str, ActiveSession],
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
    estimate_toolbar_context_usage: Callable[[str], str | None],
    update_status: Callable[[str], None],
    invalidate_prompt: Callable[[], None],
    start_turn: Callable[[str, Message | None], Thread],
) -> Thread:
    """Run forced compaction in the active-worker slot."""
    run_in_scrollback(
        lambda: print_scrollback_notice(
            scrollback_console,
            "Compacting conversation...",
        )
    )
    with state_lock:
        message_snapshot = list(state.messages)
        current_session = active_session_ref["active_session"]
        conversation_entries_snapshot = active_branch_entries(
            current_session.record.conversation_entries,
            leaf_id=current_session.record.leaf_id,
        )

    def run_compaction() -> None:
        compacted = force_compact_history(
            agent,
            message_snapshot,
            conversation_entries=conversation_entries_snapshot,
        )
        if compacted is None:
            run_in_scrollback(
                lambda: print_scrollback_notice(
                    scrollback_console,
                    "Nothing to compact right now.",
                )
            )
        else:
            _persist_prompt_compaction(
                compacted,
                state=state,
                state_lock=state_lock,
                agent=agent,
                active_session=active_session_ref["active_session"],
                scrollback_console=scrollback_console,
                run_in_scrollback=run_in_scrollback,
            )
        next_prompt, next_user_message, should_finish = finish_prompt_turn(
            state=state,
            state_lock=state_lock,
            estimate_toolbar_context_usage=estimate_toolbar_context_usage,
        )
        if next_prompt is not None:
            start_turn(next_prompt, next_user_message)
            return
        if should_finish:
            invalidate_prompt()
            return
        update_status("")
        invalidate_prompt()

    thread = Thread(target=run_compaction, daemon=True)
    with state_lock:
        state.worker = thread
        state.active_stop_request = None
    update_status("Compacting conversation...")
    thread.start()
    return thread


def _interrupted_turn_messages(
    messages: list[Message],
    *,
    user_message: Message | None,
) -> list[Message]:
    interrupted_messages = [message.model_copy(deep=True) for message in messages]
    if user_message is not None:
        interrupted_messages.append(user_message.model_copy(deep=True))
    interrupted_messages.append(Message.assistant(INTERRUPTED_TURN_NOTICE))
    return interrupted_messages


def _persist_prompt_compaction(
    compacted,
    *,
    state: PromptCliState,
    state_lock: Lock,
    agent: AgentRunner,
    active_session: ActiveSession,
    scrollback_console,
    run_in_scrollback: Callable[[Callable[[], None]], None],
) -> None:
    (
        updated_messages,
        _preparation,
        _result,
        conversation_entries,
        compaction_payload,
        usage_payload,
    ) = compacted
    with state_lock:
        state.messages = updated_messages
        state.context_usage_text = format_context_usage_text(usage_payload)
    persist_session_state(
        active_session,
        agent,
        updated_messages,
        conversation_entries=conversation_entries,
    )
    run_in_scrollback(
        lambda: print_scrollback_notice(
            scrollback_console,
            format_compaction_note(compaction_payload),
        )
    )


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
            f"To resume this session run:\nyoke resume {active_session.id}",
        )
    )


def prompt_has_pending_work(state: PromptCliState, state_lock: Lock) -> bool:
    """Return whether there is active or queued prompt work."""
    with state_lock:
        return state.worker is not None or bool(state.pending_prompts)
