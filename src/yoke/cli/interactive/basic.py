"""Basic interactive CLI loop."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Thread

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import ConversationEntry, Message
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
from yoke.cli.interactive.turn_runner import TurnRunner
from yoke.cli.render import (
    InteractiveRenderer,
    OutputStream,
    build_console,
    print_agent_output,
    print_error,
    print_scrollback_notice,
    print_user_prompt,
)
from yoke.cli.runtime import (
    ActiveSession,
    AgentRunner,
    persist_session_state,
    sync_agent_skill_state_to_session,
)
from yoke.cli.runtime.turn_agent import retire_turn_agent
from yoke.session.workspace import promote_session_turn


def run_basic_interactive_cli(
    agent: AgentRunner,
    session_messages: list[Message],
    *,
    active_session: ActiveSession,
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
    state: BasicCliState, console, active_session: ActiveSession
) -> None:
    state.shutdown_requested = True
    if state.active_stop_request is not None:
        state.active_stop_request.set()
    worker = state.worker
    if worker is not None:

        def release_worker_slot() -> None:
            worker.join(timeout=0.05)
            if state.worker is worker:
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
    active_session: ActiveSession,
    agent: AgentRunner,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
    user_message: Message | None = None,
) -> Thread:
    history = list(state.messages)
    stop_event = Event()
    state.active_stop_request = stop_event
    sync_agent_skill_state_to_session(active_session, agent)

    def checkpoint_tool_result(
        turn_agent: AgentRunner,
        messages: list[Message],
        conversation_entries: list[ConversationEntry],
    ) -> None:
        if stop_event.is_set():
            return
        if isinstance(agent, RuntimeAgent) and isinstance(turn_agent, RuntimeAgent):
            agent.active_skills = [
                skill.model_copy(deep=True) for skill in turn_agent.active_skills
            ]
        state.messages = list(messages)
        persist_session_state(
            active_session,
            turn_agent,
            messages,
            conversation_entries=conversation_entries,
        )

    def run_turn() -> None:
        outcome = TurnRunner(agent, active_session).run(
            prompt,
            history,
            entries=active_session.active_entries(),
            stop_event=stop_event,
            user_message=user_message,
            indicator=renderer,
            checkpoint=checkpoint_tool_result,
        )
        result_queue.put(outcome)

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
    active_session: ActiveSession,
    agent: AgentRunner,
    console,
    stderr: OutputStream,
    renderer: InteractiveRenderer,
    result_queue: Queue[TurnSuccess | TurnFailure | TurnStopped],
) -> None:
    state.worker = None
    state.active_stop_request = None
    outcome_agent = outcome.agent or agent
    if (
        isinstance(agent, RuntimeAgent)
        and isinstance(outcome_agent, RuntimeAgent)
        and outcome_agent is not agent
    ):
        promote_session_turn(agent, outcome_agent)
        outcome_agent = agent
    if isinstance(outcome, TurnFailure):
        if outcome.messages is not None:
            state.messages = outcome.messages
            persist_session_state(
                active_session,
                outcome_agent,
                state.messages,
                conversation_entries=outcome.conversation_entries,
            )
        print_error(console, str(outcome.error))
    elif isinstance(outcome, TurnStopped):
        stopped_messages = (
            outcome.result.messages if outcome.result is not None else outcome.messages
        )
        stopped_entries = (
            outcome.result.conversation_entries
            if outcome.result is not None
            else outcome.conversation_entries
        )
        if stopped_messages is not None:
            state.messages = stopped_messages
            persist_session_state(
                active_session,
                outcome_agent,
                state.messages,
                conversation_entries=stopped_entries,
            )
        print_scrollback_notice(
            console,
            "Stopped current turn. Send a correction to continue from here.",
        )
    else:
        state.messages = outcome.result.messages
        persist_session_state(
            active_session,
            outcome_agent,
            state.messages,
            conversation_entries=outcome.result.conversation_entries,
        )
        print_agent_output(console, outcome.result.output)
        print("\a", end="", flush=True)
    retire_turn_agent(outcome.agent, primary_agent=agent)
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
    active_session: ActiveSession,
    agent: AgentRunner,
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
