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
