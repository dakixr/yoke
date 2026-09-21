"""Observe physical turn ownership independently of the logical session lane."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future


class WorkerTracker:
    """Count admissions until their controller and owned cleanup have finished."""

    def __init__(self) -> None:
        self._workers: dict[int, int] = {}
        self._changed = asyncio.Event()

    def admit(self, turn_id: int) -> None:
        self._workers[turn_id] = 1

    def release(self, turn_id: int) -> None:
        remaining = self._workers[turn_id] - 1
        if remaining:
            self._workers[turn_id] = remaining
        else:
            del self._workers[turn_id]
        self._changed.set()

    def retain(self, turn_id: int, completion: Future[None]) -> None:
        self._workers[turn_id] += 1
        loop = asyncio.get_running_loop()

        def finished(_done: Future[None]) -> None:
            try:
                loop.call_soon_threadsafe(self.release, turn_id)
            except RuntimeError:
                pass  # No HTTP observer remains after the event loop closes.

        completion.add_done_callback(finished)

    async def drain(self, timeout_seconds: float) -> int:
        """Return the outstanding admission count, never canceling owned work."""
        try:
            async with asyncio.timeout(timeout_seconds):
                while self._workers:
                    self._changed.clear()
                    await self._changed.wait()
        except TimeoutError:
            pass
        return len(self._workers)
