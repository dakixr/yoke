"""Terminal output gate for fullscreen prompt-toolkit applications."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock


class TerminalOutputGate:
    """Track whether a fullscreen UI currently owns the terminal."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_count = 0

    @property
    def active(self) -> bool:
        """Return whether output is currently suppressed."""
        with self._lock:
            return self._active_count > 0

    @contextmanager
    def suppressing(self) -> Iterator[None]:
        """Suppress terminal writes until the fullscreen UI exits."""
        with self._lock:
            self._active_count += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_count = max(0, self._active_count - 1)


terminal_output_gate = TerminalOutputGate()


def is_fullscreen_output_suppressed() -> bool:
    """Return whether terminal output is gated by a fullscreen UI."""
    return terminal_output_gate.active


@contextmanager
def suppress_terminal_output_for_fullscreen() -> Iterator[None]:
    """Mark terminal output unavailable during a fullscreen application."""
    with terminal_output_gate.suppressing():
        yield
