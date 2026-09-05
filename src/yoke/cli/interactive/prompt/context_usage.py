"""Coalesced background context-usage estimation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Lock, Thread

from yoke.cli.interactive.common import PromptCliState

logger = logging.getLogger(__name__)


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
            self._start_worker()

    def _start_worker(self) -> None:
        Thread(
            target=self._run,
            daemon=True,
            name="yoke-context-usage",
        ).start()

    def _run(self) -> None:
        restart_worker = False
        try:
            self._process_requests()
        finally:
            with self._lock:
                self._running = False
                if self._pending is not None:
                    self._running = True
                    restart_worker = True
            if restart_worker:
                self._start_worker()

    def _process_requests(self) -> None:
        while True:
            with self._lock:
                request, self._pending = self._pending, None
            if request is None:
                return
            editor_text, revision, turn_id = request
            try:
                usage = self._estimate(editor_text)
            except Exception:
                logger.exception("Failed to estimate context usage.")
                continue
            with self._state_lock:
                if (
                    self._state.context_usage_revision != revision
                    or self._state.active_turn_id != turn_id
                ):
                    continue
                self._state.context_usage_text = usage
            self._invalidate()
