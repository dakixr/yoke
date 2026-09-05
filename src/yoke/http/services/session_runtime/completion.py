"""Physical worker completion handling after an asyncio controller is cancelled."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
import logging
from typing import TypeVar


LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


def retain_cancelled_worker(
    worker: Future[T],
    *,
    loop: asyncio.AbstractEventLoop,
    retire_agent: Callable[[object | None], object],
    release_slot: Callable[[], None],
) -> None:
    """Reap a worker outcome and release its slot at physical completion.

    This path is only for cancellation of the asyncio controller task. Logical
    user interruption keeps its existing immediate lane release and lets the
    still-live controller receive and reap the eventual worker outcome.
    """

    def completed(done: Future[T]) -> None:
        try:
            outcome = done.result()
        except BaseException:  # executor cancellation has no owned outcome
            LOGGER.exception("Cancelled HTTP controller worker did not return.")
        else:
            retire_agent(getattr(outcome, "agent", None))
        try:
            loop.call_soon_threadsafe(release_slot)
        except RuntimeError:
            # A closed loop has no remaining admission lane to release. The
            # daemon resource retirement above remains independent of it.
            pass

    worker.add_done_callback(completed)
