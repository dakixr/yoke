"""Physical worker completion independent of asyncio controller cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
import logging
from typing import TypeVar

from yoke.http.services.session_runtime.reaper import retire_resource

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


def retain_cancelled_worker(
    worker: Future[T],
    *,
    loop: asyncio.AbstractEventLoop,
    retire_agent: Callable[[object | None], object],
    release_slot: Callable[[], None],
    release_resources: Callable[[], None] | None = None,
    completed: Callable[[], None] | None = None,
) -> None:
    """Reap a worker outcome and release its slot at physical completion.

    Logical user interruption keeps its immediate lane release. The controller
    can join this completion during normal finalization or leave it owned by
    callbacks when canceled.
    """

    def release_owned() -> None:
        try:
            if release_resources is not None:
                release_resources()
        finally:
            if completed is not None:
                completed()

    def worker_completed(done: Future[T]) -> None:
        cleanup: object = None
        try:
            try:
                outcome = done.result()
            except BaseException:  # executor cancellation has no owned outcome
                LOGGER.exception("HTTP controller worker did not return.")
                agent = None
            else:
                agent = getattr(outcome, "agent", None)
            try:
                cleanup = retire_agent(agent)
            except BaseException:
                LOGGER.exception("HTTP worker retirement failed.")
                cleanup = _retry_retirement(retire_agent, agent)
        finally:
            try:
                if isinstance(cleanup, Future):
                    cleanup.add_done_callback(lambda _done: release_owned())
                else:
                    release_owned()
            finally:
                try:
                    loop.call_soon_threadsafe(release_slot)
                except RuntimeError:
                    pass  # A closed loop has no admission lane to release.

    worker.add_done_callback(worker_completed)


def _retry_retirement(
    retire_agent: Callable[[object | None], object], agent: object | None
) -> Future[None]:
    """Do not acknowledge completion when cleanup ownership transfer failed."""
    transferred = False
    cleanup: object = None

    def attempt() -> bool:
        nonlocal transferred, cleanup
        if not transferred:
            cleanup = retire_agent(agent)
            transferred = True
        return not isinstance(cleanup, Future) or cleanup.done()

    return retire_resource(attempt)
