"""Coalesced background context-usage estimation."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock, Thread

from yoke.cli.interactive.common import PromptCliState


class ContextUsageWorker:
    """Run at most one estimate and retain only the newest pending request."""

    def __init__(
        self,
        *,
        state: PromptCliState,
        state_lock: Lock,
        estimate: Callable[[str], str | None],
        invalidate: Callable[[], None],
    ) -> None:
        self._state = state
        self._state_lock = state_lock
        self._estimate = estimate
        self._invalidate = invalidate
        self._lock = Lock()
        self._pending: tuple[str, int, int] | None = None
        self._running = False

    def submit(self, editor_text: str) -> None:
        """Queue the newest estimate request without blocking the caller."""
        with self._state_lock:
            self._state.context_usage_revision += 1
            revision = self._state.context_usage_revision
            turn_id = self._state.active_turn_id
        start_worker = False
        with self._lock:
            self._pending = (editor_text, revision, turn_id)
            if not self._running:
                self._running = True
                start_worker = True
        if start_worker:
            Thread(
                target=self._run,
                daemon=True,
                name="yoke-context-usage",
            ).start()

    def _run(self) -> None:
        while True:
            with self._lock:
                request = self._pending
                self._pending = None
                if request is None:
                    self._running = False
                    return
            editor_text, revision, turn_id = request
            usage = self._estimate(editor_text)
            with self._state_lock:
                if (
                    self._state.context_usage_revision != revision
                    or self._state.active_turn_id != turn_id
                ):
                    continue
                self._state.context_usage_text = usage
            self._invalidate()
