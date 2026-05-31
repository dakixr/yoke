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
    assert (Keys.Up,) in bindings
    assert (Keys.Down,) in bindings
    assert (Keys.Left,) in bindings
    assert (Keys.Right,) in bindings
