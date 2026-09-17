"""Cooperative admission and settlement for cancellable process calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import threading
from typing import TypeVar

from anyio import CancelScope


T = TypeVar("T")


async def settle(operation: Callable[[], Awaitable[T]], cancel: threading.Event) -> T:
    """Signal cancellation, then join work and its cleanup before propagating it."""

    async def invoke() -> T:
        return await operation()

    worker = asyncio.create_task(invoke())
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        # AnyIO cancellation is level-triggered. asyncio.shield alone does not
        # protect the joining task from an enclosing cancelled CancelScope.
        with CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Repeated Task.cancel() must not orphan the worker either.
                    continue
                except Exception:
                    break
        if not worker.cancelled():
            worker.exception()
        raise


@asynccontextmanager
async def admission(
    semaphore: asyncio.Semaphore, cancelled: Callable[[], bool]
) -> AsyncIterator[bool]:
    """Do not occupy a slot or start queued work after cooperative cancellation."""
    acquired = False
    try:
        while not cancelled():
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
                acquired = True
                break
            except TimeoutError:
                continue
        yield acquired and not cancelled()
    finally:
        if acquired:
            semaphore.release()


def cancelled_result() -> dict[str, object]:
    return {"ok": False, "reason": "cancelled", "cancelled": True}
