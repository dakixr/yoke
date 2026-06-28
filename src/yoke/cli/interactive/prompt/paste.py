"""Prompt-toolkit paste handling for the interactive CLI."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from collections.abc import Iterable
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import cast

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding.key_processor import KeyPress

WINDOWS_PASTE_FRAGMENT_POLL_SECONDS = 0.002
WINDOWS_PASTE_FRAGMENT_SETTLE_SECONDS = 0.03
WINDOWS_PASTE_CONTINUATION_SECONDS = 0.12
WINDOWS_VIRTUAL_KEY_V = 0x56


class PromptInputPatchable(Protocol):
    """Prompt-toolkit input object that can be patched for paste handling."""

    console_input_reader: object | None
    _yoke_multiline_paste_patch: bool

    def read_keys(self) -> list[KeyPress]:
        """Read key presses from the terminal input."""
        ...


class PromptSessionPatchable(Protocol):
    """Prompt-toolkit session shape needed for multiline paste patching."""

    _input: PromptInputPatchable
    app: Any


def _windows_paste_compat_keys(keys: Iterable[KeyPress]) -> list[KeyPress]:
    """Coalesce multiline Win32 paste bursts into bracketed paste events."""
    from prompt_toolkit.key_binding.key_processor import KeyPress
    from prompt_toolkit.keys import Keys

    pending: list[KeyPress] = []
    normalized: list[KeyPress] = []

    def flush_pending() -> None:
        nonlocal pending
        if not pending:
            return
        text_count = sum(
            1 for key_press in pending if not isinstance(key_press.key, Keys)
        )
        multiline_paste = any(
            key_press.key in {Keys.ControlJ, Keys.ControlM}
            for key_press in pending[:-1]
        )
        if text_count >= 1 and multiline_paste:
            payload = "".join(key_press.data for key_press in pending)
            payload = payload.replace("\r\n", "\n").replace("\r", "\n")
            normalized.append(KeyPress(Keys.BracketedPaste, payload))
        else:
            normalized.extend(pending)
        pending = []

    for key_press in keys:
        if key_press.key == Keys.BracketedPaste:
            flush_pending()
            payload = key_press.data.replace("\r\n", "\n").replace("\r", "\n")
            normalized.append(KeyPress(Keys.BracketedPaste, payload))
            continue
        if not isinstance(key_press.key, Keys) or key_press.key in {
            Keys.ControlJ,
            Keys.ControlM,
        }:
            pending.append(key_press)
            continue
        flush_pending()
        normalized.append(key_press)
    flush_pending()
    return normalized


def _windows_key_is_line_break(key_press: KeyPress) -> bool:
    """Return whether a key press represents a pasted line break."""
    from prompt_toolkit.keys import Keys

    return key_press.key in {Keys.ControlJ, Keys.ControlM}


def _windows_key_is_text_or_line_break(key_press: KeyPress) -> bool:
    """Return whether a key press can be part of a raw text paste burst."""
    from prompt_toolkit.keys import Keys

    return not isinstance(
        key_press.key,
        Keys,
    ) or _windows_key_is_line_break(key_press)


def _windows_key_burst_is_text_or_line_break(
    keys: Iterable[KeyPress],
) -> bool:
    """Return whether every key in a burst can be raw pasted text."""
    return all(_windows_key_is_text_or_line_break(key) for key in keys)


def _windows_key_burst_has_text(keys: Iterable[KeyPress]) -> bool:
    """Return whether a key burst includes printable text."""
    from prompt_toolkit.keys import Keys

    return any(not isinstance(key_press.key, Keys) for key_press in keys)


def _windows_key_burst_ends_with_line_break(keys: list[KeyPress]) -> bool:
    """Return whether a key burst ends with a pasted line break."""
    return bool(keys) and _windows_key_is_line_break(keys[-1])


def _windows_key_burst_has_multiline_text(
    keys: Iterable[KeyPress],
) -> bool:
    """Return whether raw text continues after a line break in a key burst."""
    from prompt_toolkit.keys import Keys

    saw_line_break = False
    for key_press in keys:
        if _windows_key_is_line_break(key_press):
            saw_line_break = True
            continue
        if saw_line_break and not isinstance(key_press.key, Keys):
            return True
    return False


def _windows_key_burst_is_paste_prefix(keys: list[KeyPress]) -> bool:
    """Return whether a raw key burst may be the first line of a paste."""
    return (
        _windows_key_burst_is_text_or_line_break(keys)
        and _windows_key_burst_has_text(keys)
        and _windows_key_burst_ends_with_line_break(keys)
        and not _windows_key_burst_has_multiline_text(keys)
    )


def _read_windows_paste_key_burst(
    read_keys: Callable[[], list[KeyPress]],
) -> list[KeyPress]:
    """Read extra raw key chunks when a paste split looks like Enter."""
    keys = read_keys()
    if not _windows_key_burst_is_paste_prefix(keys):
        return keys

    deadline = time.monotonic() + WINDOWS_PASTE_FRAGMENT_SETTLE_SECONDS
    while True:
        extra_keys = read_keys()
        if extra_keys:
            keys.extend(extra_keys)
            if not _windows_key_burst_is_text_or_line_break(extra_keys):
                break
            if _windows_key_burst_has_multiline_text(keys):
                deadline = time.monotonic()
                continue
            deadline = time.monotonic() + WINDOWS_PASTE_FRAGMENT_SETTLE_SECONDS
            continue

        if _windows_key_burst_has_multiline_text(keys):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(WINDOWS_PASTE_FRAGMENT_POLL_SECONDS, remaining))
    return keys


def _coerce_windows_text_burst_to_bracketed_paste(
    keys: list[KeyPress],
) -> list[KeyPress]:
    """Coerce a raw text continuation into one bracketed paste event."""
    from prompt_toolkit.key_binding.key_processor import KeyPress
    from prompt_toolkit.keys import Keys

    if not keys or not _windows_key_burst_is_text_or_line_break(keys):
        return _windows_paste_compat_keys(keys)
    payload = "".join(key_press.data for key_press in keys)
    payload = payload.replace("\r\n", "\n").replace("\r", "\n")
    return [KeyPress(Keys.BracketedPaste, payload)]


def _windows_key_burst_has_bracketed_paste(
    keys: Iterable[KeyPress],
) -> bool:
    """Return whether a key burst contains a bracketed paste event."""
    from prompt_toolkit.keys import Keys

    return any(key_press.key == Keys.BracketedPaste for key_press in keys)


def patch_prompt_toolkit_input_for_multiline_paste(
    prompt_session: PromptSession[str],
) -> None:
    """Patch Win32 prompt-toolkit input for single-prompt multiline paste."""
    if sys.platform != "win32":
        return
    if not hasattr(prompt_session, "_input") or not hasattr(prompt_session, "app"):
        return
    session = cast(PromptSessionPatchable, prompt_session)
    prompt_input = session._input
    if prompt_input is None:
        return
    if not hasattr(prompt_input, "_yoke_multiline_paste_patch"):
        prompt_input._yoke_multiline_paste_patch = False
    if prompt_input.console_input_reader is None:
        return
    _patch_windows_ctrl_v_by_virtual_key(prompt_input.console_input_reader)
    if prompt_input._yoke_multiline_paste_patch:
        return
    read_keys = prompt_input.read_keys
    paste_continuation_until = 0.0

    def patched_read_keys() -> list[KeyPress]:
        nonlocal paste_continuation_until
        keys = _read_windows_paste_key_burst(read_keys)
        now = time.monotonic()
        if now <= paste_continuation_until:
            normalized = _coerce_windows_text_burst_to_bracketed_paste(keys)
        else:
            normalized = _windows_paste_compat_keys(keys)
        if _windows_key_burst_has_bracketed_paste(normalized):
            paste_continuation_until = (
                time.monotonic() + WINDOWS_PASTE_CONTINUATION_SECONDS
            )
        return normalized

    cast(Any, prompt_input).read_keys = patched_read_keys
    cast(Any, prompt_input)._yoke_multiline_paste_patch = True
    session.app.input = prompt_input


def _patch_windows_ctrl_v_by_virtual_key(console_input_reader: object) -> None:
    """Normalize Windows Ctrl+V from the virtual key before layout translation."""
    if getattr(console_input_reader, "_yoke_ctrl_v_key_patch", False):
        return
    event_to_key_presses = getattr(
        console_input_reader,
        "_event_to_key_presses",
        None,
    )
    if event_to_key_presses is None:
        return

    def patched_event_to_key_presses(ev) -> list[KeyPress]:
        if _windows_event_is_ctrl_v(ev, console_input_reader):
            from prompt_toolkit.key_binding.key_processor import KeyPress
            from prompt_toolkit.keys import Keys

            return [KeyPress(Keys.ControlV, "\x16")]
        return event_to_key_presses(ev)

    cast(Any, console_input_reader)._event_to_key_presses = patched_event_to_key_presses
    cast(Any, console_input_reader)._yoke_ctrl_v_key_patch = True


def _windows_event_is_ctrl_v(ev: object, console_input_reader: object) -> bool:
    """Return whether a Win32 key event is Ctrl+V by virtual key code."""
    control_key_state = int(getattr(ev, "ControlKeyState", 0))
    ctrl_mask = int(getattr(console_input_reader, "LEFT_CTRL_PRESSED", 0)) | int(
        getattr(console_input_reader, "RIGHT_CTRL_PRESSED", 0)
    )
    alt_mask = int(getattr(console_input_reader, "LEFT_ALT_PRESSED", 0)) | int(
        getattr(console_input_reader, "RIGHT_ALT_PRESSED", 0)
    )
    return (
        bool(getattr(ev, "KeyDown", False))
        and int(getattr(ev, "VirtualKeyCode", -1)) == WINDOWS_VIRTUAL_KEY_V
        and bool(control_key_state & ctrl_mask)
        and not bool(control_key_state & alt_mask)
    )
