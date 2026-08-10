from __future__ import annotations

import asyncio
import io
import importlib
from threading import Thread, get_ident

from yoke.cli.interactive.prompt import scrollback as scrollback_module
from yoke.cli.interactive.prompt.scrollback import BatchedScrollback
from yoke.cli.render import build_console
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)


class _ThreadRecordingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writer_threads: list[int] = []

    def write(self, text: str) -> int:
        self.writer_threads.append(get_ident())
        return super().write(text)


class _Output:
    def get_size(self):
        return type("Size", (), {"columns": 100})()


class _App:
    output = _Output()


def test_scrollback_renders_off_loop_and_writes_one_ordered_batch(monkeypatch) -> None:
    stream = _ThreadRecordingStream()
    scrollback = BatchedScrollback(
        build_console(stream),
        batch_delay_seconds=0.01,
    )
    render_threads: list[int] = []
    terminal_threads: list[int] = []
    original_render = scrollback_module._render_batch

    def record_render(*args, **kwargs) -> str:
        render_threads.append(get_ident())
        return original_render(*args, **kwargs)

    async def run_in_terminal(callback, **_kwargs):
        terminal_threads.append(get_ident())
        callback()

    monkeypatch.setattr(scrollback_module, "_render_batch", record_render)
    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )
    monkeypatch.setattr(run_in_terminal_module, "run_in_terminal", run_in_terminal)

    async def exercise() -> None:
        ui_thread = get_ident()
        pump = asyncio.create_task(scrollback.run(_App()))
        await asyncio.sleep(0)

        def emit_from_worker() -> None:
            scrollback.emit("user", "one")
            scrollback.emit("notice", "two")

        worker = Thread(target=emit_from_worker)
        worker.start()
        worker.join()
        scrollback.emit("agent", "three")
        await scrollback.flush(_App())
        scrollback.close()
        await asyncio.wait_for(pump, timeout=1)

        assert len(terminal_threads) == 1
        assert terminal_threads == [ui_thread]
        assert render_threads and all(thread != ui_thread for thread in render_threads)
        assert stream.writer_threads and all(
            thread == ui_thread for thread in stream.writer_threads
        )

    asyncio.run(exercise())
    output = stream.getvalue()
    assert output.index("one") < output.index("two") < output.index("three")


def test_scrollback_waits_until_fullscreen_output_is_released(monkeypatch) -> None:
    stream = _ThreadRecordingStream()
    scrollback = BatchedScrollback(
        build_console(stream),
        batch_delay_seconds=0.005,
    )
    terminal_writes: list[None] = []

    async def run_in_terminal(callback, **_kwargs):
        terminal_writes.append(None)
        callback()

    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )
    monkeypatch.setattr(run_in_terminal_module, "run_in_terminal", run_in_terminal)

    async def exercise() -> None:
        pump = asyncio.create_task(scrollback.run(_App()))
        await asyncio.sleep(0)
        with suppress_terminal_output_for_fullscreen():
            scrollback.emit("notice", "after fullscreen")
            await asyncio.sleep(0.07)
            assert scrollback.pending_count == 1
            assert terminal_writes == []
        await scrollback.flush(_App())
        scrollback.close()
        await asyncio.wait_for(pump, timeout=1)

    asyncio.run(exercise())
    assert terminal_writes == [None]
    assert "after fullscreen" in stream.getvalue()
