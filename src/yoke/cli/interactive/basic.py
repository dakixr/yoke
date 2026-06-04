"""Basic interactive CLI loop."""

from __future__ import annotations

import time
from queue import Empty
from queue import Queue
from threading import Thread

from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.cli.config import CLIArgs
from yoke.cli.config import RUN_ERRORS
from yoke.cli.image_input import attach_standalone_prompt_image_paths
from yoke.cli.image_input import build_user_message
from yoke.cli.interactive.common import BasicCliState
from yoke.cli.interactive.common import InputFunc
from yoke.cli.interactive.common import InputInterrupted
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import TurnFailure
from yoke.cli.interactive.common import TurnStopped
from yoke.cli.interactive.common import TurnSuccess
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive.common import (
    partial_conversation_entries_from_error,
)
from yoke.cli.interactive.common import partial_messages_from_error
from yoke.cli.render import InteractiveRenderer
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.render import print_agent_output
from yoke.cli.render import print_error
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_session_scrollback
from yoke.cli.render import print_user_prompt
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime import ensure_session_title
from yoke.cli.runtime import execute_turn
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import sync_agent_skill_state_to_session


def run_basic_interactive_cli(
    args: CLIArgs,
    agent: AgentRunner,
    session_messages: list[Message],
    *,
    active_session: ActiveSession,
    input_func: InputFunc,
    stdout: OutputStream,
    stderr: OutputStream,
    replay_session: bool = False,
) -> int:
    """Run the fallback interactive CLI without prompt-toolkit."""
    del args
    state = BasicCliState(messages=list(session_messages), pending_prompts=[])
    stdout_console = build_console(stdout)
    renderer = InteractiveRenderer(stdout)
    input_queue: Queue[str | None | InputInterrupted] = Queue()
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped] = Queue()
    _start_basic_input_reader(input_func, input_queue)
    if stdout_console.is_terminal:
        renderer.print_intro()
    _seed_basic_session_if_needed(
        state=state,
        replay_session=replay_session,
        console=stdout_console,
        active_session=active_session,
        agent=agent,
        stderr=stderr,
        renderer=renderer,
        result_queue=result_queue,
    )
    while True:
        try:
            active_session = _drain_basic_input_queue(
                state=state,
                input_queue=input_queue,
                active_session=active_session,
                agent=agent,
                console=stdout_console,
                stderr=stderr,
                renderer=renderer,
                result_queue=result_queue,
            )
        except Empty:
            pass
        try:
            outcome = result_queue.get_nowait()
        except Empty:
            outcome = None
        if outcome is not None:
            _handle_basic_outcome(
                outcome,
                state=state,
                active_session=active_session,
                agent=agent,
                console=stdout_console,
                stderr=stderr,
                renderer=renderer,
                result_queue=result_queue,
            )
        if (
            state.shutdown_requested
            and state.worker is None
            and not state.pending_prompts
        ):
            if state.input_closed:
                stdout_console.print()
            return 0
        time.sleep(0.05)


def _request_basic_exit(
    state: BasicCliState,
    console,
    active_session: ActiveSession,
) -> None:
    state.shutdown_requested = True
    if state.exit_notice_emitted:
        return
    state.exit_notice_emitted = True
    print_scrollback_notice(
        console,
        f"To resume this session run:\nyoke resume {active_session.id}",
    )


def _start_basic_turn(
    prompt: str,
    *,
    state: BasicCliState,
    active_session: ActiveSession,
    agent: AgentRunner,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
    user_message: Message | None = None,
) -> Thread:
    history = list(state.messages)
    sync_agent_skill_state_to_session(active_session, agent)

    def run_turn() -> None:
        try:
            ensure_session_title(active_session, agent, prompt, stderr=stderr)
            result = execute_turn(
                agent,
                prompt,
                history,
                stderr=stderr,
                indicator=renderer,
                user_message=user_message,
                conversation_entries=active_branch_entries(
                    active_session.record.conversation_entries,
                    leaf_id=active_session.record.leaf_id,
                ),
            )
            if result.status == "stopped":
                result_queue.put(TurnStopped(result=result))
                return
        except RUN_ERRORS as exc:
            result_queue.put(
                TurnFailure(
                    error=exc,
                    messages=partial_messages_from_error(exc),
                    conversation_entries=partial_conversation_entries_from_error(exc),
                )
            )
            return
        result_queue.put(TurnSuccess(result=result))

    thread = Thread(target=run_turn, daemon=True)
    thread.start()
    return thread


def _drain_basic_input_queue(
    *,
    state: BasicCliState,
    input_queue: Queue[str | None | InputInterrupted],
    active_session: ActiveSession,
    agent: AgentRunner,
    console,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> ActiveSession:
    while True:
        prompt = input_queue.get_nowait()
        if isinstance(prompt, InputInterrupted):
            _request_basic_exit(state, console, active_session)
            continue
        if prompt is None:
            state.input_closed = True
            state.shutdown_requested = True
            continue
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            _request_basic_exit(state, console, active_session)
            continue
        handled, state.messages, active_session = handle_slash_command(
            prompt,
            agent=agent,
            active_session=active_session,
            messages=state.messages,
            console=console,
            pending_images=state.pending_images,
        )
        if handled:
            continue
        prompt, dropped_images = attach_standalone_prompt_image_paths(
            prompt,
            root=active_session.root,
        )
        state.pending_images.extend(dropped_images)
        if state.worker is None and not state.pending_prompts:
            print_user_prompt(console, prompt)
            pending_images = [image.path for image in state.pending_images]
            state.pending_images.clear()
            state.worker = _start_basic_turn(
                prompt,
                state=state,
                active_session=active_session,
                agent=agent,
                stderr=stderr,
                renderer=renderer,
                result_queue=result_queue,
                user_message=build_user_message(
                    prompt,
                    image_paths=pending_images,
                ),
            )
            continue
        pending_images = [image.path for image in state.pending_images]
        state.pending_images.clear()
        state.pending_prompts.append(
            PendingPrompt(
                prompt,
                user_message=build_user_message(
                    prompt,
                    image_paths=pending_images,
                ),
            )
        )


def _handle_basic_outcome(
    outcome: TurnSuccess | TurnFailure | TurnStopped,
    *,
    state: BasicCliState,
    active_session: ActiveSession,
    agent: AgentRunner,
    console,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> None:
    state.worker = None
    if isinstance(outcome, TurnFailure):
        if outcome.messages is not None:
            state.messages = outcome.messages
            persist_session_state(
                active_session,
                agent,
                state.messages,
                conversation_entries=outcome.conversation_entries,
            )
        print_error(console, str(outcome.error))
    elif isinstance(outcome, TurnStopped):
        if outcome.result is not None:
            state.messages = outcome.result.messages
            persist_session_state(
                active_session,
                agent,
                state.messages,
                conversation_entries=outcome.result.conversation_entries,
            )
        print_scrollback_notice(
            console,
            "Stopped current turn. Send a correction to continue from here.",
        )
    else:
        state.messages = outcome.result.messages
        persist_session_state(
            active_session,
            agent,
            state.messages,
            conversation_entries=outcome.result.conversation_entries,
        )
        print_agent_output(console, outcome.result.output)
        print("\a", end="", flush=True)
    if not state.pending_prompts:
        return
    pending = state.pending_prompts.pop(0)
    next_prompt = pending.prompt
    print_user_prompt(console, next_prompt)
    state.worker = _start_basic_turn(
        next_prompt,
        state=state,
        active_session=active_session,
        agent=agent,
        stderr=stderr,
        renderer=renderer,
        result_queue=result_queue,
        user_message=pending.user_message,
    )


def _start_basic_input_reader(
    input_func: InputFunc,
    input_queue: Queue[str | None | InputInterrupted],
) -> None:
    def read_input() -> None:
        while True:
            try:
                raw = input_func("› ")
            except KeyboardInterrupt:
                input_queue.put(InputInterrupted())
                return
            except (EOFError, StopIteration):
                input_queue.put(None)
                return
            prompt = raw.strip()
            input_queue.put(prompt)
            if prompt.lower() in {"exit", "quit"}:
                return

    Thread(target=read_input, daemon=True).start()


def _seed_basic_session_if_needed(
    *,
    state: BasicCliState,
    replay_session: bool,
    console,
    active_session: ActiveSession,
    agent: AgentRunner,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> None:
    if replay_session and state.messages:
        print_session_scrollback(console, state.messages)
    if replay_session or not state.messages or state.messages[-1].role != "user":
        return
    seeded_message = state.messages[-1].model_copy(deep=True)
    seeded_prompt = seeded_message.text_content() or ""
    state.messages = state.messages[:-1]
    if not seeded_prompt:
        return
    print_user_prompt(console, seeded_prompt)
    state.worker = _start_basic_turn(
        seeded_prompt,
        state=state,
        active_session=active_session,
        agent=agent,
        stderr=stderr,
        renderer=renderer,
        result_queue=result_queue,
        user_message=seeded_message,
    )
