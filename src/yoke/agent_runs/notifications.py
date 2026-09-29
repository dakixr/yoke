"""One lazy worker and one pending invalidation, independent of reporting ACKs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
import threading


class Notifier:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._listeners: set[Callable[[], None]] = set()
        self._pending = False
        self._closed = False
        self._thread: threading.Thread | None = None

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._condition:
            if not self._closed:
                self._listeners.add(callback)

        def unsubscribe() -> None:
            with self._condition:
                self._listeners.discard(callback)

        return unsubscribe

    def notify(self) -> None:
        with self._condition:
            if not self._closed:
                self._schedule()

    def _schedule(self) -> None:
        if not self._listeners:
            return
        self._pending = True
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="yoke-agent-run-notify"
            )
            self._thread.start()
        self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if not self._pending:
                    self._listeners.clear()
                    return
                self._pending = False
                callbacks = tuple(self._listeners)
            for callback in callbacks:
                with self._condition:
                    subscribed = callback in self._listeners
                if subscribed:
                    with suppress(Exception):
                        callback()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            # Deliver the final observation asynchronously too. A stuck listener
            # cannot hold host cleanup; the worker exits when callbacks return.
            self._schedule()
            self._closed = True
            self._condition.notify()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.1)
