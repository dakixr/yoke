"""Tests for prompt-toolkit interactive key bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.keys import (
    register_prompt_toolkit_key_bindings,
)

PASTE_KEY_SEQUENCES = (("c-v",), ("escape", "v"))


@dataclass
class _ClipboardData:
    text: str = ""


class _Clipboard:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def get_data(self) -> _ClipboardData:
        return _ClipboardData(self._text)


class _Buffer:
    def __init__(self) -> None:
        self.inserted = ""
        self.text = ""
        self.complete_state = None
        self.validation_count = 0

    def insert_text(self, text: str) -> None:
        self.inserted += text

    def validate_and_handle(self) -> None:
        self.validation_count += 1


class _App:
    def __init__(self, clipboard_text: str = "") -> None:
        self.clipboard = _Clipboard(clipboard_text)


class _Event:
    def __init__(self, clipboard_text: str = "") -> None:
        self.app = _App(clipboard_text)
        self.current_buffer = _Buffer()


class _KeyBindings:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, ...], Callable[[_Event], None]] = {}

    def add(
        self,
        *keys: str,
    ) -> Callable[[Callable[[_Event], None]], Callable[[_Event], None]]:
        def decorator(
            handler: Callable[[_Event], None],
        ) -> Callable[[_Event], None]:
            self.handlers[keys] = handler
            return handler

        return decorator


def _registered_key_bindings(
    monkeypatch,
    *,
    cycle_thinking_effort: Callable[[], str | None] | None = None,
    request_clipboard_paste: Callable[[object, str], None] | None = None,
    open_tool_inspector: Callable[[], None] | None = None,
    open_process_inspector: Callable[[], None] | None = None,
    open_queue_manager: Callable[[str], None] | None = None,
) -> _KeyBindings:
    key_bindings = _KeyBindings()
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.register_completion_menu_key_bindings",
        lambda _key_bindings: None,
    )
    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=PromptCliState(messages=[], pending_prompts=[]),
        stop_active_turn=lambda: False,
        remove_last_image=lambda: None,
        cycle_thinking_effort=cycle_thinking_effort or (lambda: None),
        request_clipboard_paste=request_clipboard_paste
        or (lambda _buffer, _text: None),
        update_status=lambda _message: None,
        open_tool_inspector=open_tool_inspector,
        open_process_inspector=open_process_inspector,
        open_queue_manager=open_queue_manager,
    )
    return key_bindings


def test_inspector_shortcuts_have_distinct_handlers(monkeypatch) -> None:
    """Each inspector shortcut opens only its advertised inspector."""
    opened: list[str] = []
    key_bindings = _registered_key_bindings(
        monkeypatch,
        open_tool_inspector=lambda: opened.append("tool"),
        open_process_inspector=lambda: opened.append("process"),
    )

    key_bindings.handlers[("c-x", "o")](_Event())
    key_bindings.handlers[("c-x", "c-p")](_Event())

    assert opened == ["tool", "process"]
    assert ("c-o",) not in key_bindings.handlers


def test_queue_manager_uses_modal_prefix(monkeypatch) -> None:
    """Queue manager opens under Ctrl+X and leaves the legacy chord free."""
    preserved: list[str] = []
    key_bindings = _registered_key_bindings(
        monkeypatch,
        open_queue_manager=preserved.append,
    )
    event = _Event()
    event.current_buffer.text = "draft"

    key_bindings.handlers[("c-x", "q")](event)

    assert preserved == ["draft"]
    assert event.current_buffer.text == "/queue"
    assert event.current_buffer.validation_count == 1
    assert ("c-q",) not in key_bindings.handlers


def test_shift_tab_variants_cycle_without_submitting(monkeypatch) -> None:
    """Both terminal encodings of Shift+Tab only change thinking effort."""
    efforts: list[str] = []

    def cycle_effort() -> str:
        effort = "medium" if not efforts else "high"
        efforts.append(effort)
        return effort

    key_bindings = _registered_key_bindings(
        monkeypatch,
        cycle_thinking_effort=cycle_effort,
    )

    for key_sequence in (("s-tab",), ("escape", "tab")):
        event = _Event()
        key_bindings.handlers[key_sequence](event)
        if event.current_buffer.validation_count != 0:
            raise AssertionError
    if efforts != ["medium", "high"]:
        raise AssertionError


def test_paste_shortcuts_delegate_without_probing_clipboard(monkeypatch) -> None:
    """Paste keys only capture in-memory text and delegate expensive work."""
    requests: list[tuple[object, str]] = []
    key_bindings = _registered_key_bindings(
        monkeypatch,
        request_clipboard_paste=lambda buffer, text: requests.append((buffer, text)),
    )

    for key_sequence in PASTE_KEY_SEQUENCES:
        event = _Event("prompt clipboard")
        key_bindings.handlers[key_sequence](event)
        assert requests[-1] == (event.current_buffer, "prompt clipboard")
        assert event.current_buffer.inserted == ""

    assert len(requests) == 2
