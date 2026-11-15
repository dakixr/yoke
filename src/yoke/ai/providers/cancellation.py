"""Cooperative cancellation watchers for blocking provider transports."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from yoke.ai.providers.base import ProviderCancelledError


@contextmanager
def cancellation_watcher(
    cancel_requested: Callable[[], bool] | None,
    abort: Callable[[], None],
    *,
    rebuild: Callable[[], None] | None = None,
    repeat: bool = False,
) -> Iterator[None]:
    """Abort on cancellation, then optionally rebuild after the request unwinds.

    A repeating abort supports lazy clients that can be recreated during retries.
    Close-only transports stop their watcher after the first cancellation.
    """
    finished = threading.Event()
    aborted = threading.Event()

    def watch() -> None:
        if cancel_requested is None:
            return
        while not finished.wait(0.05):
            if cancel_requested():
                aborted.set()
                abort()
                if not repeat:
                    return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield
        if cancel_requested is not None and cancel_requested():
            raise ProviderCancelledError()
    finally:
        finished.set()
        # Join before rebuilding so a late abort cannot close the replacement.
        watcher.join()
        if aborted.is_set() and rebuild is not None:
            rebuild()
