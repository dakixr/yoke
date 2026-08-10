"""Batched scrollback output for the persistent prompt application."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from io import StringIO
from threading import Lock
from typing import Literal, Protocol, cast

from rich.console import Console
from rich.text import Text

from yoke.cli.render import print_scrollback_agent
from yoke.cli.render import print_scrollback_commentary
from yoke.cli.render import print_scrollback_error
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_scrollback_tool
from yoke.cli.render import print_scrollback_user
from yoke.cli.render import print_scrollback_warning
from yoke.cli.render import print_tool_response_divider
from yoke.cli.runtime.terminal_output_gate import is_fullscreen_output_suppressed

ScrollbackKind = Literal[
    "agent",
    "bell",
    "commentary",
    "divider",
    "error",
    "notice",
    "raw",
    "summary",
    "tool",
    "user",
    "warning",
]
ColorSystem = Literal["auto", "standard", "256", "truecolor", "windows"]


@dataclass(frozen=True, slots=True)
class ScrollbackItem:
    """One ordered scrollback record awaiting terminal output."""

    kind: ScrollbackKind
    text: str = ""
    failed: bool = False


class ScrollbackSink(Protocol):
    """UI-independent interface for emitting ordered scrollback records."""

    def emit(
        self,
        kind: ScrollbackKind,
        text: str = "",
        *,
        failed: bool = False,
    ) -> None:
        """Queue one scrollback record."""


class BatchedScrollback:
    """Render scrollback off-loop and commit it in bounded terminal batches."""

    def __init__(
        self,
        console: Console,
        *,
        batch_delay_seconds: float = 0.025,
        max_batch_items: int = 128,
        max_batch_characters: int = 256 * 1024,
    ) -> None:
        self._console = console
        self._batch_delay = batch_delay_seconds
        self._max_batch_items = max_batch_items
        self._max_batch_characters = max_batch_characters
        self._pending: deque[ScrollbackItem] = deque()
        self._pending_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._flush_lock: asyncio.Lock | None = None
        self._closed = False

    def emit(
        self,
        kind: ScrollbackKind,
        text: str = "",
        *,
        failed: bool = False,
    ) -> None:
        """Queue one item from any thread without touching the terminal."""
        with self._pending_lock:
            if self._closed:
                return
            self._pending.append(ScrollbackItem(kind, text, failed))
        loop = self._loop
        wakeup = self._wakeup
        if loop is not None and wakeup is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wakeup.set)

    async def run(self, app) -> None:
        """Drain pending records until the application requests shutdown."""
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._wakeup.set()
        while not self._closed:
            await self._wakeup.wait()
            self._wakeup.clear()
            await asyncio.sleep(self._batch_delay)
            if is_fullscreen_output_suppressed():
                await asyncio.sleep(0.05)
                self._wakeup.set()
                continue
            await self._flush_one_batch(app)
            if self.pending_count:
                self._wakeup.set()

    async def flush(self, app) -> None:
        """Write all records currently queued before application shutdown."""
        while self.pending_count:
            if is_fullscreen_output_suppressed():
                await asyncio.sleep(0.01)
                continue
            await self._flush_one_batch(app)

    def close(self) -> None:
        """Reject future output after the application has exited."""
        with self._pending_lock:
            self._closed = True
        loop = self._loop
        wakeup = self._wakeup
        if loop is not None and wakeup is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wakeup.set)

    def drain_sync(self, *, width: int) -> None:
        """Synchronously drain records after the prompt application exits."""
        while self.pending_count:
            items = self._take_batch()
            rendered = _render_batch(
                items,
                width=width,
                terminal=self._console.is_terminal,
                color_system=self._console.color_system,
            )
            if rendered:
                self._write(rendered)

    @property
    def pending_count(self) -> int:
        """Return the number of records awaiting output."""
        with self._pending_lock:
            return len(self._pending)

    async def _flush_one_batch(self, app) -> None:
        flush_lock = self._flush_lock
        if flush_lock is None:
            return
        async with flush_lock:
            items = self._take_batch()
            if not items:
                return
            width = max(1, app.output.get_size().columns)
            rendered = await asyncio.to_thread(
                _render_batch,
                items,
                width=width,
                terminal=self._console.is_terminal,
                color_system=self._console.color_system,
            )
            if not rendered:
                return
            from prompt_toolkit.application.run_in_terminal import run_in_terminal

            await run_in_terminal(lambda: self._write(rendered))

    def _take_batch(self) -> list[ScrollbackItem]:
        with self._pending_lock:
            items: list[ScrollbackItem] = []
            characters = 0
            while self._pending and len(items) < self._max_batch_items:
                next_item = self._pending[0]
                if (
                    items
                    and characters + len(next_item.text) > self._max_batch_characters
                ):
                    break
                items.append(self._pending.popleft())
                characters += len(next_item.text)
            return items

    def _write(self, rendered: str) -> None:
        stream = self._console.file
        stream.write(rendered)
        stream.flush()


def _render_batch(
    items: list[ScrollbackItem],
    *,
    width: int,
    terminal: bool,
    color_system: str | None,
) -> str:
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=terminal,
        color_system=(cast(ColorSystem | None, color_system) if terminal else None),
        width=width,
        no_color=False,
        highlight=False,
    )
    for item in items:
        _render_item(console, item)
    return stream.getvalue()


def _render_item(console: Console, item: ScrollbackItem) -> None:
    if item.kind == "agent":
        print_scrollback_agent(console, item.text)
    elif item.kind == "bell":
        console.file.write("\a")
    elif item.kind == "commentary":
        print_scrollback_commentary(console, item.text)
    elif item.kind == "divider":
        print_tool_response_divider(console)
    elif item.kind == "error":
        print_scrollback_error(console, item.text)
    elif item.kind == "notice":
        print_scrollback_notice(console, item.text)
    elif item.kind == "raw":
        console.file.write(item.text)
    elif item.kind == "summary":
        console.print(Text(item.text, style="dim"))
    elif item.kind == "tool":
        print_scrollback_tool(console, item.text, failed=item.failed)
    elif item.kind == "user":
        print_scrollback_user(console, item.text)
    else:
        print_scrollback_warning(console, item.text)
