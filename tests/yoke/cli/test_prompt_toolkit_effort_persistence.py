from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,D100,D103,F405,S101

from .support import *  # noqa: F403, F405
from .support import FakeAgent as BaseFakeAgent


def test_prompt_toolkit_persists_effort_on_ctrl_c_exit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))

    class ProviderWithEffort(CatalogProvider):
        provider_name = "codex"

    class FakeAgent(BaseFakeAgent):
        def __init__(self) -> None:
            super().__init__()
            self.provider = ProviderWithEffort(
                type(
                    "Config",
                    (),
                    {"model": "gpt-5.4", "reasoning_effort": "medium"},
                )()
            )
            self.available_skills = []
            self.active_skills = []

    import prompt_toolkit

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

        def prompt(self, *_args, **kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            raise EOFError

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    active_session = active_session_for(tmp_path)
    active_session.record.provider_name = "codex"
    active_session.record.model_id = "gpt-5.4"

    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path), reasoning_effort="high"),
        FakeAgent(),
        [Message.user("hello")],
        active_session=active_session,
    )

    assert exit_code == 0
    assert SessionStore().load(active_session.id).reasoning_effort == "high"


def test_prompt_toolkit_exit_persists_state_thinking_effort(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))

    class ProviderWithoutEffortChange(CatalogProvider):
        provider_name = "codex"

    class FakeAgent(BaseFakeAgent):
        def __init__(self) -> None:
            super().__init__()
            self.provider = ProviderWithoutEffortChange(
                type(
                    "Config",
                    (),
                    {"model": "gpt-5.4", "reasoning_effort": "medium"},
                )()
            )
            self.available_skills = []
            self.active_skills = []

    import prompt_toolkit

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

        def prompt(self, *_args, **kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                key_bindings = kwargs["key_bindings"]
                binding = next(
                    binding
                    for binding in key_bindings.bindings
                    if binding.keys == ("s-tab",)
                )
                binding.handler(type("Event", (), {})())
                raise KeyboardInterrupt
            raise EOFError

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    active_session = active_session_for(tmp_path)
    active_session.record.provider_name = "codex"
    active_session.record.model_id = "gpt-5.4"

    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path), reasoning_effort="medium"),
        FakeAgent(),
        [Message.user("hello")],
        active_session=active_session,
    )

    assert exit_code == 0
    assert SessionStore().load(active_session.id).reasoning_effort == "high"
