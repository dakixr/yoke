"""Independent daemon cleanup for HTTP resources that outlive their runtime."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import logging
from threading import Thread
from time import sleep

LOGGER = logging.getLogger(__name__)
RETRY_SECONDS = 0.05


def retire_resource(attempt: Callable[[], bool]) -> Future[None]:
    """Retain one resource until its sequential cleanup attempts succeed.

    Each outstanding retirement has one daemon thread, so a blocking provider
    close cannot stall another session. Cancelling an asyncio waiter does not
    discard the resource or start a concurrent cleanup attempt.
    """
    completion: Future[None] = Future()
    Thread(
        target=_retry,
        args=(attempt, completion),
        daemon=True,
        name="yoke-http-resource-reaper",
    ).start()
    return completion


def _retry(attempt: Callable[[], bool], completion: Future[None]) -> None:
    while True:
        try:
            complete = attempt()
        except BaseException:
            LOGGER.exception("Retired HTTP resource cleanup attempt failed.")
            complete = False
        if complete:
            if not completion.done():
                completion.set_result(None)
            return
        sleep(RETRY_SECONDS)
