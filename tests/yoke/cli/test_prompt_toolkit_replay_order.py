from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,D100,D103,F405,S101

from .support import *  # noqa: F403, F405


def test_prompt_toolkit_resume_prints_intro_before_replayed_messages(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    import importlib
    import prompt_toolkit

    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )

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
            return "quit"

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    monkeypatch.setattr(
        run_in_terminal_module,
        "run_in_terminal",
        lambda func, *args, **kwargs: func(),
    )
    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        FakeAgent(),
        [Message.user("old prompt"), Message.assistant("old answer")],
        active_session=active_session_for(tmp_path),
        replay_session=True,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.index("user old prompt") < output.index("old answer")
