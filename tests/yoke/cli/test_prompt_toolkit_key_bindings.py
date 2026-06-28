from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,D100,D103,F405,S101

from .support import *  # noqa: F403, F405
from .support import FakeAgent as BaseFakeAgent


def test_prompt_toolkit_double_escape_stops_active_turn(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeAgent(BaseFakeAgent):
        def run(
            self,
            prompt: str,
            messages: Sequence[Message] | None = None,
            *,
            on_event=None,
            stop_requested=None,
        ) -> AgentResult:
            del on_event, stop_requested
            conversation = list(messages or [])
            conversation.append(Message.user(prompt))
            conversation.append(Message.assistant("ok"))
            return AgentResult(output="ok", messages=conversation, iterations=1)

    import prompt_toolkit
    from prompt_toolkit.keys import Keys

    session_holder: dict[str, Any] = {}

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
            self.calls = 0
            self.kwargs = kwargs
            session_holder["session"] = self

        def prompt(self, *_args, **kwargs) -> str:
            self.calls += 1
            session_holder["prompt_kwargs"] = kwargs
            return "quit" if self.calls == 1 else (_ for _ in ()).throw(EOFError())

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        FakeAgent(),
        [],
        active_session=active_session_for(tmp_path),
        on_app_created=lambda session: session_holder.setdefault("created", session),
    )

    bindings = {
        binding.keys
        for binding in session_holder["prompt_kwargs"]["key_bindings"].bindings
    }

    assert exit_code == 0
    assert session_holder["created"] is session_holder["session"]
    assert session_holder["session"].kwargs["erase_when_done"] is True
    assert "style" not in session_holder["session"].kwargs
    assert "bottom_toolbar" in session_holder["prompt_kwargs"]
    assert session_holder["prompt_kwargs"]["multiline"] is True
    assert "completer" in session_holder["prompt_kwargs"]
    assert session_holder["prompt_kwargs"]["complete_while_typing"] is True
    assert session_holder["prompt_kwargs"]["reserve_space_for_menu"] == 6
    assert "style" in session_holder["prompt_kwargs"]
    assert ("escape", "escape") in bindings
    assert (Keys.ControlI,) in bindings
    assert (Keys.ControlM,) in bindings
    assert (Keys.ControlJ,) in bindings
    assert (Keys.ControlO,) in bindings
    assert (Keys.Up,) in bindings
    assert (Keys.Down,) in bindings
    assert (Keys.Left,) in bindings
    assert (Keys.Right,) in bindings


def test_prompt_toolkit_ctrl_o_runs_inspector_in_executor(
    tmp_path: Path, monkeypatch
) -> None:
    import importlib
    import prompt_toolkit
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from prompt_toolkit.keys import Keys
    import yoke.cli.interactive.prompt as prompt_module

    run_calls: list[dict[str, object]] = []
    session_holder: dict[str, Any] = {}
    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )

    class FakeLoop:
        def call_soon_threadsafe(self, callback) -> None:
            callback()

    class FakeApp:
        loop = FakeLoop()

        def invalidate(self) -> None:
            return None

    class FakePromptSession:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.app = FakeApp()
            self.calls = 0
            session_holder["session"] = self

        def prompt(self, *_args, **kwargs) -> str:
            self.calls += 1
            session_holder["prompt_kwargs"] = kwargs
            return "quit"

    def fake_run_in_terminal(func, *args, **kwargs) -> None:
        del func, args
        run_calls.append(kwargs)

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    monkeypatch.setattr(
        run_in_terminal_module,
        "run_in_terminal",
        fake_run_in_terminal,
    )
    monkeypatch.setattr(
        prompt_module,
        "open_live_tool_inspector",
        lambda _entries_provider, *, trace_store=None: None,
    )

    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        BaseFakeAgent(),
        [],
        active_session=active_session_for(tmp_path),
    )
    binding = next(
        item
        for item in session_holder["prompt_kwargs"]["key_bindings"].bindings
        if item.keys == (Keys.ControlO,)
    )

    class FakeEvent:
        pass

    binding.handler(cast(KeyPressEvent, FakeEvent()))

    assert exit_code == 0
    assert run_calls == [{"in_executor": True}]


def test_tool_inspector_vim_navigation_key_bindings() -> None:
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from yoke.cli.interactive.tools.inspector import ToolInspectorState
    from yoke.cli.interactive.tools.inspector import _register_tool_inspector_keys
    from yoke.cli.interactive.tools.trace import ToolTraceEntry

    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(tool_call_id="call-1", tool_name="read"),
            ToolTraceEntry(tool_call_id="call-2", tool_name="edit"),
        ]
    )
    key_bindings = KeyBindings()
    _register_tool_inspector_keys(
        key_bindings,
        state=state,
        visible_entries=lambda: state.entries,
        any_key="a",
    )

    def press(key: str) -> None:
        binding = next(item for item in key_bindings.bindings if item.keys == (key,))

        class FakeApp:
            def invalidate(self) -> None:
                return None

            def exit(self) -> None:
                return None

        class FakeEvent:
            app = FakeApp()

        binding.handler(cast(KeyPressEvent, FakeEvent()))

    press("g")
    assert state.selected_index == 0
    press("G")
    assert state.selected_index == 1
    press("l")
    assert state.active_pane == "detail"
    press("j")
    assert state.detail_scroll == 1
    press("k")
    assert state.detail_scroll == 0
    press("h")
    assert state.active_pane == "sidebar"
    press("h")
    assert state.active_pane == "detail"
    press("l")
    assert state.active_pane == "sidebar"
