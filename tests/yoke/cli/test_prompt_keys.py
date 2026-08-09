"""Tests for prompt-toolkit interactive key bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yoke.cli.image_input import ImageAttachment
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
    attach_image: Callable[[ImageAttachment], None] | None = None,
    cycle_thinking_effort: Callable[[], str | None] | None = None,
    open_tool_inspector: Callable[[], None] | None = None,
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
        attach_image=attach_image or (lambda _attachment: None),
        remove_last_image=lambda: None,
        resolve_image_path=lambda _raw: (_ for _ in ()).throw(ValueError),
        cycle_thinking_effort=cycle_thinking_effort or (lambda: None),
        update_status=lambda _message: None,
        open_tool_inspector=open_tool_inspector,
    )
    return key_bindings


def test_process_inspector_shortcuts_share_handler(monkeypatch) -> None:
    """Ctrl+O and Ctrl+X Ctrl+P both open the process inspector."""
    opened: list[None] = []
    key_bindings = _registered_key_bindings(
        monkeypatch,
        open_tool_inspector=lambda: opened.append(None),
    )

    for key_sequence in (("c-o",), ("c-x", "c-p")):
        key_bindings.handlers[key_sequence](_Event())

    assert len(opened) == 2
    assert key_bindings.handlers[("c-o",)] is key_bindings.handlers[("c-x", "c-p")]


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


def test_paste_shortcuts_share_handler(monkeypatch) -> None:
    """Ctrl+V and Alt+V run the same paste action."""
    key_bindings = _registered_key_bindings(monkeypatch)

    if key_bindings.handlers[("c-v",)] is not key_bindings.handlers[("escape", "v")]:
        raise AssertionError


def test_paste_shortcuts_prefer_prompt_toolkit_clipboard(monkeypatch) -> None:
    """Paste shortcuts prefer prompt-toolkit clipboard text when available."""
    key_bindings = _registered_key_bindings(monkeypatch)
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.paste_image_from_clipboard",
        lambda: None,
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.paste_text_from_clipboard",
        lambda: "os clipboard",
    )
    for key_sequence in PASTE_KEY_SEQUENCES:
        event = _Event("prompt clipboard")
        key_bindings.handlers[key_sequence](event)
        if event.current_buffer.inserted != "prompt clipboard":
            raise AssertionError


def test_paste_shortcuts_fall_back_to_os_clipboard(monkeypatch) -> None:
    """Paste shortcuts fall back to OS clipboard text."""
    key_bindings = _registered_key_bindings(monkeypatch)
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.paste_image_from_clipboard",
        lambda: None,
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.paste_text_from_clipboard",
        lambda: "dictated text",
    )
    for key_sequence in PASTE_KEY_SEQUENCES:
        event = _Event()
        key_bindings.handlers[key_sequence](event)
        if event.current_buffer.inserted != "dictated text":
            raise AssertionError


def test_paste_shortcuts_attach_clipboard_images(monkeypatch) -> None:
    """Both paste shortcuts attach an image from the clipboard."""
    attachment = ImageAttachment(path=Path("clipboard.png"))
    attached: list[ImageAttachment] = []
    inserted: list[ImageAttachment] = []
    key_bindings = _registered_key_bindings(
        monkeypatch,
        attach_image=attached.append,
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.paste_image_from_clipboard",
        lambda: attachment,
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.keys.insert_attachment_reference",
        lambda _buffer, image: inserted.append(image),
    )

    for key_sequence in PASTE_KEY_SEQUENCES:
        key_bindings.handlers[key_sequence](_Event())

    if attached != [attachment, attachment]:
        raise AssertionError
    if inserted != [attachment, attachment]:
        raise AssertionError
