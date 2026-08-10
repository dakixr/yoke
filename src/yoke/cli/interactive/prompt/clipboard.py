"""Nonblocking clipboard probing for the prompt-toolkit UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread

from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import paste_image_from_clipboard
from yoke.cli.image_input import paste_text_from_clipboard


@dataclass(slots=True, frozen=True)
class ClipboardPasteResult:
    """Clipboard content resolved away from the prompt-toolkit UI thread."""

    attachment: ImageAttachment | None = None
    text: str = ""
    error: Exception | None = None


def start_clipboard_paste(
    prompt_toolkit_text: str,
    *,
    on_result: Callable[[ClipboardPasteResult], None],
) -> Thread:
    """Probe the clipboard in a worker and report exactly one result."""

    def run() -> None:
        try:
            attachment = paste_image_from_clipboard()
            if attachment is not None:
                result = ClipboardPasteResult(attachment=attachment)
            else:
                result = ClipboardPasteResult(
                    text=prompt_toolkit_text or paste_text_from_clipboard()
                )
        except Exception as exc:  # Clipboard backends raise platform-specific errors.
            result = ClipboardPasteResult(error=exc)
        on_result(result)

    worker = Thread(target=run, daemon=True, name="yoke-clipboard-paste")
    worker.start()
    return worker
