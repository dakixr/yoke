"""Tests for nonblocking prompt clipboard resolution."""

from __future__ import annotations

import threading
from pathlib import Path

from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.prompt.clipboard import ClipboardPasteResult
from yoke.cli.interactive.prompt.clipboard import start_clipboard_paste


def test_clipboard_probe_runs_outside_caller_thread(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    results: list[ClipboardPasteResult] = []

    def probe_image() -> None:
        started.set()
        release.wait(timeout=5)
        return None

    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.clipboard.paste_image_from_clipboard",
        probe_image,
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.clipboard.paste_text_from_clipboard",
        lambda: "OS clipboard",
    )

    def record_result(result: ClipboardPasteResult) -> None:
        results.append(result)
        completed.set()

    worker = start_clipboard_paste(
        "",
        on_result=record_result,
    )

    assert started.wait(timeout=1)
    assert worker.is_alive()
    assert not completed.is_set()
    release.set()
    worker.join(timeout=1)
    assert results == [ClipboardPasteResult(text="OS clipboard")]


def test_clipboard_image_wins_without_reading_text(monkeypatch) -> None:
    attachment = ImageAttachment(path=Path("clipboard.png"))
    results: list[ClipboardPasteResult] = []
    text_probed = threading.Event()
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.clipboard.paste_image_from_clipboard",
        lambda: attachment,
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.clipboard.paste_text_from_clipboard",
        lambda: (text_probed.set(), "text")[1],
    )

    worker = start_clipboard_paste("prompt text", on_result=results.append)
    worker.join(timeout=1)

    assert results == [ClipboardPasteResult(attachment=attachment)]
    assert not text_probed.is_set()


def test_clipboard_worker_reports_backend_errors(monkeypatch) -> None:
    error = RuntimeError("clipboard unavailable")
    results: list[ClipboardPasteResult] = []
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.clipboard.paste_image_from_clipboard",
        lambda: (_ for _ in ()).throw(error),
    )

    worker = start_clipboard_paste("", on_result=results.append)
    worker.join(timeout=1)

    assert results == [ClipboardPasteResult(error=error)]
