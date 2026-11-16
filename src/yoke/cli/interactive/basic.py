"""Basic interactive CLI loop."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Thread

from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.interactive.turn_stream import render_turn_events
from yoke.cli.interactive.session_observer import SessionObserver
from yoke.agent.models import Message
from yoke.cli.image_input import build_user_message
from yoke.cli.interactive.command_host import CommandCallbacks
from yoke.cli.interactive.commands import handle_slash_command
from yoke.cli.interactive.common import (
    BasicCliState,
    InputFunc,
    InputInterrupted,
    PendingPrompt,
    TurnFailure,
    TurnStopped,
    TurnSuccess,
    session_resume_notice,
)
from yoke.cli.interactive.events import WakeQueue, start_basic_input_reader
from yoke.cli.interactive.seeding import seed_session
from yoke.cli.render import (
    InteractiveRenderer,
    OutputStream,
    build_console,
    print_agent_output,
    print_error,
    print_scrollback_notice,
    print_user_prompt,
)
from yoke.cli.backend.daemon import DaemonActiveSession


def run_basic_interactive_cli(
    agent: DaemonSessionBackend,
    session_messages: list[Message],
    *,
    active_session: DaemonActiveSession,
    input_func: InputFunc,
    stdout: OutputStream,
    stderr: OutputStream,
    replay_session: bool = False,
    replay_messages: list[Message] | None = None,
    replay_notice: str | None = None,
) -> int:
    """Run the fallback interactive CLI without prompt-toolkit."""
    state = BasicCliState(messages=list(session_messages), pending_prompts=[])
    stdout_console = build_console(stdout)
    renderer = InteractiveRenderer(stdout)
    input_queue: Queue[str | None | InputInterrupted] = WakeQueue(state.wakeup)
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped] = WakeQueue(
        state.wakeup
    )
    start_basic_input_reader(input_func, input_queue)
    if stdout_console.is_terminal:
        renderer.print_intro()
    with SessionObserver(
        agent,
        renderer=renderer,
        user=lambda text: print_user_prompt(stdout_console, text),
        output=renderer.print_agent_output,
        notice=lambda text: print_scrollback_notice(stdout_console, text),
        messages=lambda messages: setattr(state, "messages", messages),
    ):
        _seed_basic_session_if_needed(
            state=state,
            replay_session=replay_session,
            replay_messages=replay_messages,
            replay_notice=replay_notice,
            console=stdout_console,
            active_session=active_session,
            agent=agent,
            stderr=stderr,
            renderer=renderer,
            result_queue=result_queue,
        )
        while True:
            state.wakeup.clear()
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
            if (
                state.shutdown_requested
                and state.worker is None
                and not state.pending_prompts
            ):
                if state.input_closed:
                    stdout_console.print()
                return 0
            if input_queue.empty() and result_queue.empty():
                state.wakeup.wait()


def _request_basic_exit(
    state: BasicCliState, console, active_session: DaemonActiveSession
) -> None:
    state.shutdown_requested = True
    if state.active_stop_request is not None:
        state.active_stop_request.set()
        active_session.backend.interrupt()
    worker = state.worker
    if worker is not None:

        def release_worker_slot() -> None:
            worker.join(timeout=0.05)
            if worker.is_alive() and state.worker is worker:
                state.worker = None
            state.wakeup.set()

        Thread(target=release_worker_slot, daemon=True, name="yoke-basic-stop").start()
    if state.exit_notice_emitted:
        return
    state.exit_notice_emitted = True
    print_scrollback_notice(
        console,
        session_resume_notice(active_session.id),
    )


def _start_basic_turn(
    prompt: str,
    *,
    state: BasicCliState,
    active_session: DaemonActiveSession,
    agent: DaemonSessionBackend,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
    user_message: Message | None = None,
) -> Thread:
    stop_event = Event()
    state.active_stop_request = stop_event
    backend = agent

    def run_turn() -> None:
        submitted = backend.submit(prompt, user_message=user_message)
        render_turn_events(submitted, renderer)
        result_queue.put(submitted.result())

    thread = Thread(target=run_turn, daemon=True)
    thread.start()
    return thread


def _drain_basic_input_queue(
    *,
    state: BasicCliState,
    input_queue: Queue[str | None | InputInterrupted],
    active_session: DaemonActiveSession,
    agent: DaemonSessionBackend,
    console,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> DaemonActiveSession:
    while True:
        try:
            prompt = input_queue.get_nowait()
        except Empty:
            return active_session
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
        submitted_prompts: list[str] = []
        handled, state.messages, active_session = handle_slash_command(
            prompt,
            host=CommandCallbacks(
                basic_mode=True, on_submit_prompt=submitted_prompts.append
            ),
            agent=agent,
            active_session=active_session,
            messages=state.messages,
            console=console,
            pending_images=state.pending_images,
        )
        if handled:
            if not submitted_prompts:
                continue
            prompt = submitted_prompts[0]
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
            return active_session
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
    active_session: DaemonActiveSession,
    agent: DaemonSessionBackend,
    console,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> None:
    state.worker = None
    state.active_stop_request = None
    if isinstance(outcome, TurnFailure):
        if outcome.messages is not None:
            state.messages = outcome.messages
        print_error(console, str(outcome.error))
    elif isinstance(outcome, TurnStopped):
        stopped_messages = (
            outcome.result.messages if outcome.result is not None else outcome.messages
        )
        if stopped_messages is not None:
            state.messages = stopped_messages
        print_scrollback_notice(
            console,
            "Stopped current turn. Send a correction to continue from here.",
        )
    else:
        state.messages = outcome.result.messages
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


def _seed_basic_session_if_needed(
    *,
    state: BasicCliState,
    replay_session: bool,
    replay_messages: list[Message] | None,
    replay_notice: str | None,
    console,
    active_session: DaemonActiveSession,
    agent: DaemonSessionBackend,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> None:
    def start_seed(prompt: str, message: Message) -> Thread:
        print_user_prompt(console, prompt)
        state.worker = _start_basic_turn(
            prompt,
            state=state,
            active_session=active_session,
            agent=agent,
            stderr=stderr,
            renderer=renderer,
            result_queue=result_queue,
            user_message=message,
        )
        return state.worker

    seed_session(
        state,
        replay_session=replay_session,
        replay_messages=replay_messages,
        replay_notice=replay_notice,
        console=console,
        start_turn=start_seed,
    )
