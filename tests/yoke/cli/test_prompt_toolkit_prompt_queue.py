from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,ANN202,D100,D103,F405,S101

from .support import *  # noqa: F403, F405


def test_prompt_toolkit_queues_without_injecting_scrollback_until_processed(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    @dataclass
    class SlowAgent:
        supports_message_history = True
        supports_user_message = False

        def run(
            self,
            prompt: str,
            messages: list[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del on_event, stop_requested
            time.sleep(0.05)
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            conversation.append(Message.assistant(f"done {prompt}"))
            return AgentResult(
                output=f"done {prompt}", messages=conversation, iterations=1
            )

    import importlib
    import prompt_toolkit
    from prompt_toolkit.keys import Keys

    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )

    prompts = iter(["first", "second", "quit"])

    class FakeLoop:
        def call_soon_threadsafe(self, callback) -> None:
            callback()

    class FakeApp:
        def __init__(self) -> None:
            self.loop = FakeLoop()

        def invalidate(self) -> None:
            return None

    class FakePromptSession:
        def __init__(self, *args, **kwargs) -> None:
            self.app = FakeApp()

        def prompt(self, *_args, **kwargs) -> str:
            prompt = next(prompts)
            if prompt == "second":
                binding = next(
                    item
                    for item in kwargs["key_bindings"].bindings
                    if item.keys == (Keys.ControlI,)
                )

                class FakeBuffer:
                    def validate_and_handle(self) -> None:
                        return None

                class FakeEvent:
                    current_buffer = FakeBuffer()
                    app = self.app

                binding.handler(FakeEvent())
            return prompt

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    monkeypatch.setattr(
        run_in_terminal_module,
        "run_in_terminal",
        lambda func, *args, **kwargs: func(),
    )
    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        SlowAgent(),
        [],
        active_session=active_session_for(tmp_path),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "queued 1:" not in out
    assert out.count("user first") == 1
    assert out.count("user second") == 1


def test_prompt_queue_persistence_round_trips_pending_prompts(tmp_path: Path) -> None:
    from yoke.cli.image_input import ImageAttachment
    from yoke.cli.interactive.common import PendingPrompt
    from yoke.cli.interactive.queue.persistence import load_prompt_queue
    from yoke.cli.interactive.queue.persistence import persist_prompt_queue

    active_session = active_session_for(tmp_path)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"fake")
    prompts = [
        PendingPrompt("queued", kind="queued", id="one"),
        PendingPrompt("steer", kind="steering", id="two", paused=True),
    ]

    persist_prompt_queue(
        active_session,
        prompts,
        [ImageAttachment(path=image_path)],
    )

    restored_prompts, restored_images = load_prompt_queue(active_session)
    assert [
        (prompt.id, prompt.prompt, prompt.kind, prompt.paused)
        for prompt in restored_prompts
    ] == [
        ("one", "queued", "queued", False),
        ("two", "steer", "steering", True),
    ]
    assert [image.path for image in restored_images] == [image_path]


def test_finish_prompt_turn_skips_paused_queue_items() -> None:
    from threading import Lock

    from yoke.cli.interactive.common import PendingPrompt
    from yoke.cli.interactive.common import PromptCliState
    from yoke.cli.interactive.prompt.turns import finish_prompt_turn

    state = PromptCliState(
        messages=[],
        pending_prompts=[
            PendingPrompt("paused", paused=True),
            PendingPrompt("active"),
        ],
    )

    next_prompt, _user_message, should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        estimate_toolbar_context_usage=lambda _prompt: None,
    )

    assert next_prompt == "active"
    assert should_finish is False
    assert [prompt.prompt for prompt in state.pending_prompts] == ["paused"]


def test_finish_prompt_turn_prioritizes_steering_items() -> None:
    from threading import Lock

    from yoke.cli.interactive.common import PendingPrompt
    from yoke.cli.interactive.common import PromptCliState
    from yoke.cli.interactive.prompt.turns import finish_prompt_turn

    state = PromptCliState(
        messages=[],
        pending_prompts=[
            PendingPrompt("queued 1"),
            PendingPrompt("queued 2"),
            PendingPrompt("steer", kind="steering"),
        ],
    )

    next_prompt, _user_message, should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        estimate_toolbar_context_usage=lambda _prompt: None,
    )

    assert next_prompt == "steer"
    assert should_finish is False
    assert [prompt.prompt for prompt in state.pending_prompts] == [
        "queued 1",
        "queued 2",
    ]


def test_steer_active_turn_retires_stopped_turn_immediately(tmp_path: Path) -> None:
    from threading import Event
    from threading import Lock
    from threading import Thread

    from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control
    from yoke.cli.interactive.queue.persistence import load_prompt_queue

    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    stop_event = Event()
    stop_event.set()
    state.worker = Thread(target=lambda: None)
    state.active_stop_request = stop_event
    state.active_turn_id = 7
    state.status_message = "Cancelling model request"
    active_session = active_session_for(tmp_path)
    lock = Lock()
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda _status: None,
    )
    control = create_prompt_toolkit_control(
        state=state,
        agent=FakeAgent(),
        active_session_ref={"active_session": active_session},
        renderer=renderer,
        scrollback_console=build_console(io.StringIO()),
        state_lock=lock,
        estimate_toolbar_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        run_in_scrollback=lambda callback: callback(),
    )

    assert control.steer_active_turn("use config.py instead", None) is True

    worker = state.worker
    assert worker is not None
    worker.join(timeout=1)
    assert state.active_turn_id == 8
    assert state.abandoned_turn_ids == {7}
    assert state.pending_prompts == []
    restored_prompts, _restored_images = load_prompt_queue(active_session)
    assert restored_prompts == []


def test_prompt_turn_persists_queue_after_dequeue(tmp_path: Path) -> None:
    from threading import Event
    from threading import Lock

    from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control
    from yoke.cli.interactive.queue.persistence import load_prompt_queue
    from yoke.cli.interactive.queue.persistence import persist_prompt_queue

    second_started = Event()
    finish_second = Event()

    @dataclass
    class BlockingSecondAgent:
        supports_message_history = True
        supports_user_message = False
        prompts: list[str] = field(default_factory=list)

        def run(
            self,
            prompt: str,
            messages: Sequence[Message] | None = None,
            *,
            on_event: Any = None,
            stop_requested: Any = None,
        ) -> AgentResult:
            del on_event, stop_requested
            self.prompts.append(prompt)
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            if prompt == "second":
                second_started.set()
                assert finish_second.wait(timeout=2)
            conversation.append(Message.assistant(f"done {prompt}"))
            return AgentResult(
                output=f"done {prompt}", messages=conversation, iterations=1
            )

    active_session = active_session_for(tmp_path)
    state = PromptCliState(
        messages=[],
        pending_prompts=[PendingPrompt("second")],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    persist_prompt_queue(active_session, state.pending_prompts, state.pending_images)
    agent = BlockingSecondAgent()
    lock = Lock()
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda _status: None,
    )
    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session},
        renderer=renderer,
        scrollback_console=build_console(io.StringIO()),
        state_lock=lock,
        estimate_toolbar_context_usage=lambda _prompt: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _status: None,
        run_in_scrollback=lambda callback: callback(),
    )

    first_worker = control.start_turn("first", None)
    assert second_started.wait(timeout=2)

    restored_prompts, restored_images = load_prompt_queue(active_session)
    assert restored_prompts == []
    assert restored_images == []
    finish_second.set()
    first_worker.join(timeout=2)
    with lock:
        current_worker = state.worker
    if current_worker is not None:
        current_worker.join(timeout=2)
    assert agent.prompts == ["first", "second"]


def test_first_non_steering_index_places_steering_before_queued() -> None:
    from yoke.cli.interactive.common import PendingPrompt
    from yoke.cli.interactive.queue.manager import _first_non_steering_index

    prompts = [
        PendingPrompt("queued 1"),
        PendingPrompt("queued 2"),
    ]

    assert _first_non_steering_index(prompts) == 0

    prompts.insert(0, PendingPrompt("steer", kind="steering"))
    assert _first_non_steering_index(prompts) == 1


def test_queue_manager_edit_exits_before_prompting(monkeypatch) -> None:
    import yoke.cli.interactive.queue.manager as queue_manager

    from yoke.cli.interactive.common import PendingPrompt

    calls: list[str] = []
    original_run = queue_manager._run_queue_manager

    def fake_run(state, *, prompts, changed):
        del prompts, changed
        if not calls:
            calls.append("request")
            return queue_manager._QueueManagerEditRequest(0)
        calls.append("close")
        return state.prompts

    monkeypatch.setattr(queue_manager, "_run_queue_manager", fake_run)

    def edit_prompt(prompt: PendingPrompt) -> PendingPrompt:
        calls.append("edit")
        updated = prompt.copy_for_queue()
        updated.prompt = "edited"
        return updated

    result = queue_manager.open_queue_manager(
        [PendingPrompt("original")],
        edit_prompt=edit_prompt,
    )

    monkeypatch.setattr(queue_manager, "_run_queue_manager", original_run)
    assert calls == ["request", "edit", "close"]
    assert result is not None
    assert result[0].prompt == "edited"


def test_queue_edit_enter_saves_and_ctrl_j_inserts_newline() -> None:
    from prompt_toolkit.keys import Keys

    from yoke.cli.interactive.queue.manager import queue_edit_key_bindings

    key_bindings = queue_edit_key_bindings()
    keys = {binding.keys for binding in key_bindings.bindings}

    assert (Keys.ControlM,) in keys
    assert (Keys.ControlJ,) in keys
