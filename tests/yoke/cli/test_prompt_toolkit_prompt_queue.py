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
