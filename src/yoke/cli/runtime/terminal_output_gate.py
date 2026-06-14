"""Terminal output gate for fullscreen prompt-toolkit applications."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock


class TerminalOutputGate:
    """Defer terminal writes while a fullscreen UI owns the terminal."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_count = 0
        self._pending: list[Callable[[], None]] = []

    @property
    def active(self) -> bool:
        """Return whether output is currently suppressed."""
        with self._lock:
            return self._active_count > 0

    def defer(self, callback: Callable[[], None]) -> bool:
        """Queue a terminal write if a fullscreen UI is active."""
        with self._lock:
            if self._active_count <= 0:
                return False
            self._pending.append(callback)
            return True

    @contextmanager
    def suppressing(self) -> Iterator[None]:
        """Suppress terminal writes until the fullscreen UI exits."""
        with self._lock:
            self._active_count += 1
        try:
            yield
        finally:
            callbacks = self._exit_and_collect_pending()
            for callback in callbacks:
                callback()

    def _exit_and_collect_pending(self) -> list[Callable[[], None]]:
        with self._lock:
            self._active_count = max(0, self._active_count - 1)
            if self._active_count > 0:
                return []
            callbacks = list(self._pending)
            self._pending.clear()
            return callbacks


terminal_output_gate = TerminalOutputGate()


def is_fullscreen_output_suppressed() -> bool:
    """Return whether terminal output is gated by a fullscreen UI."""
    return terminal_output_gate.active


def defer_until_fullscreen_exits(callback: Callable[[], None]) -> bool:
    """Queue callback execution until the active fullscreen UI exits."""
    return terminal_output_gate.defer(callback)


@contextmanager
def suppress_terminal_output_for_fullscreen() -> Iterator[None]:
    """Defer terminal writes while a fullscreen application is running."""
    with terminal_output_gate.suppressing():
        yield
