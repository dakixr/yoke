from __future__ import annotations

# ruff: noqa: F403, F405
from typing import Protocol
from typing import cast

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from .support import *  # noqa: F403, F405
from .support import _windows_paste_compat_keys


class PatchedInput(Protocol):
    _yoke_multiline_paste_patch: bool


def test_windows_paste_compat_keys_converts_multiline_burst_to_bracketed_paste() -> (
    None
):
    normalized = _windows_paste_compat_keys(
        [
            KeyPress("f", "f"),
            KeyPress("i", "i"),
            KeyPress("r", "r"),
            KeyPress("s", "s"),
            KeyPress("t", "t"),
            KeyPress(Keys.ControlM, "\r"),
            KeyPress("s", "s"),
            KeyPress("e", "e"),
            KeyPress("c", "c"),
            KeyPress("o", "o"),
            KeyPress("n", "n"),
            KeyPress("d", "d"),
            KeyPress(Keys.ControlM, "\r"),
        ]
    )

    assert normalized == [KeyPress(Keys.BracketedPaste, "first\nsecond\n")]


def test_windows_paste_compat_keys_keeps_single_line_submit_sequence() -> None:
    normalized = _windows_paste_compat_keys(
        [
            KeyPress("h", "h"),
            KeyPress("i", "i"),
            KeyPress(Keys.ControlM, "\r"),
        ]
    )

    assert normalized == [
        KeyPress("h", "h"),
        KeyPress("i", "i"),
        KeyPress(Keys.ControlM, "\r"),
    ]


def test_patch_prompt_toolkit_input_for_multiline_paste_wraps_read_keys_once(
    monkeypatch,
) -> None:
    monkeypatch.setattr("yoke.cli.interactive.prompt_paste.sys.platform", "win32")

    read_calls: list[str] = []
    key_batches = [
        [
            KeyPress("f", "f"),
            KeyPress(Keys.ControlM, "\r"),
            KeyPress("s", "s"),
            KeyPress(Keys.ControlM, "\r"),
        ]
    ]

    class FakeInput:
        def __init__(self) -> None:
            self.console_input_reader = object()

        def read_keys(self) -> list[KeyPress]:
            read_calls.append("read")
            return key_batches.pop(0)

    class FakeApp:
        def __init__(self, prompt_input: FakeInput) -> None:
            self.input = prompt_input

    class FakePromptSession:
        def __init__(self) -> None:
            self._input = FakeInput()
            self.app = FakeApp(self._input)

    session = FakePromptSession()
    patch_prompt_toolkit_input_for_multiline_paste(cast(Any, session))
    first_read = session._input.read_keys()
    patch_prompt_toolkit_input_for_multiline_paste(cast(Any, session))

    assert first_read == [KeyPress(Keys.BracketedPaste, "f\ns\n")]
    assert read_calls == ["read"]
    assert session.app.input is session._input
    assert cast(PatchedInput, session._input)._yoke_multiline_paste_patch is True


def test_patch_prompt_toolkit_input_for_multiline_paste_combines_split_line(
    monkeypatch,
) -> None:
    monkeypatch.setattr("yoke.cli.interactive.prompt_paste.sys.platform", "win32")

    read_calls: list[str] = []
    key_batches = [
        [
            KeyPress("f", "f"),
            KeyPress("i", "i"),
            KeyPress("r", "r"),
            KeyPress("s", "s"),
            KeyPress("t", "t"),
            KeyPress(Keys.ControlM, "\r"),
        ],
        [
            KeyPress("s", "s"),
            KeyPress("e", "e"),
            KeyPress("c", "c"),
            KeyPress("o", "o"),
            KeyPress("n", "n"),
            KeyPress("d", "d"),
            KeyPress(Keys.ControlM, "\r"),
        ],
        [],
    ]

    class FakeInput:
        def __init__(self) -> None:
            self.console_input_reader = object()

        def read_keys(self) -> list[KeyPress]:
            read_calls.append("read")
            if not key_batches:
                return []
            return key_batches.pop(0)

    class FakeApp:
        def __init__(self, prompt_input: FakeInput) -> None:
            self.input = prompt_input

    class FakePromptSession:
        def __init__(self) -> None:
            self._input = FakeInput()
            self.app = FakeApp(self._input)

    session = FakePromptSession()
    patch_prompt_toolkit_input_for_multiline_paste(cast(Any, session))
    first_read = session._input.read_keys()

    assert first_read == [KeyPress(Keys.BracketedPaste, "first\nsecond\n")]
    assert read_calls == ["read", "read", "read"]


def test_patch_prompt_toolkit_input_for_multiline_paste_keeps_tail_as_paste(
    monkeypatch,
) -> None:
    monkeypatch.setattr("yoke.cli.interactive.prompt_paste.sys.platform", "win32")

    key_batches = [
        [
            KeyPress("f", "f"),
            KeyPress("i", "i"),
            KeyPress("r", "r"),
            KeyPress("s", "s"),
            KeyPress("t", "t"),
            KeyPress(Keys.ControlM, "\r"),
        ],
        [
            KeyPress("s", "s"),
            KeyPress("e", "e"),
            KeyPress("c", "c"),
            KeyPress("o", "o"),
            KeyPress("n", "n"),
            KeyPress("d", "d"),
        ],
        [],
        [KeyPress(Keys.ControlM, "\r")],
    ]

    class FakeInput:
        def __init__(self) -> None:
            self.console_input_reader = object()

        def read_keys(self) -> list[KeyPress]:
            if not key_batches:
                return []
            return key_batches.pop(0)

    class FakeApp:
        def __init__(self, prompt_input: FakeInput) -> None:
            self.input = prompt_input

    class FakePromptSession:
        def __init__(self) -> None:
            self._input = FakeInput()
            self.app = FakeApp(self._input)

    session = FakePromptSession()
    patch_prompt_toolkit_input_for_multiline_paste(cast(Any, session))

    first_read = session._input.read_keys()
    second_read = session._input.read_keys()

    assert first_read == [KeyPress(Keys.BracketedPaste, "first\nsecond")]
    assert second_read == [KeyPress(Keys.BracketedPaste, "\n")]


def test_run_prompt_toolkit_cli_patches_multiline_paste_input(
    tmp_path: Path, monkeypatch
) -> None:
    import prompt_toolkit

    monkeypatch.setattr("yoke.cli.interactive.prompt_paste.sys.platform", "win32")

    session_holder: dict[str, object] = {}

    class FakeInput:
        def __init__(self) -> None:
            self.console_input_reader = object()

        def read_keys(self) -> list[KeyPress]:
            return []

    class FakeLoop:
        def call_soon_threadsafe(self, callback) -> None:
            callback()

    class FakeApp:
        def __init__(self, prompt_input: FakeInput) -> None:
            self.loop = FakeLoop()
            self.input = prompt_input

        def invalidate(self) -> None:
            return None

    class FakePromptSession:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self._input = FakeInput()
            self.app = FakeApp(self._input)
            session_holder["session"] = self

        def prompt(self, *_args, **_kwargs) -> str:
            return "quit"

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)
    exit_code = run_prompt_toolkit_cli(
        CLIArgs(root=str(tmp_path)),
        FakeAgent(),
        [],
        active_session=active_session_for(tmp_path),
    )

    assert exit_code == 0
    session = cast(Any, session_holder["session"])
    assert cast(PatchedInput, session._input)._yoke_multiline_paste_patch is True
    assert session.app.input is session._input
