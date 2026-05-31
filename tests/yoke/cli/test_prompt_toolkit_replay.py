from __future__ import annotations

# ruff: noqa: F403, F405
from .support import *  # noqa: F403, F405


def test_prompt_toolkit_replay_preserves_user_image_reference_text(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    import importlib
    import prompt_toolkit

    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )

    stored_message = Message.user(
        [
            MessageTextContentPart(text="before [yoke-clipboard-h87ietvo.png] after"),
            MessageLocalImageContentPart(
                path=str(tmp_path / "yoke-clipboard-h87ietvo.png")
            ),
        ]
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
        ImageAwareAgent(),
        [stored_message],
        active_session=active_session_for(tmp_path),
        replay_session=True,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "before [yoke-clipboard-h87ietvo.png] after" in output
    assert "[Image]" not in output
