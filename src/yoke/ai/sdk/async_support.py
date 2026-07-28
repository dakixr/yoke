"""Internal asyncio bridge for Yoke's synchronous agent runtime."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable


async def run_sync_cooperatively[ResultT](
    call: Callable[[], ResultT],
    *,
    stop_event: threading.Event,
) -> ResultT:
    """Run sync work and cooperatively stop it on async interruption."""
    worker = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop_event.set()
        outcome = await _drain_worker(worker)
        if not isinstance(outcome, BaseException):
            return outcome
        raise


async def _drain_worker[ResultT](
    worker: asyncio.Task[ResultT],
) -> ResultT | BaseException:
    """Wait through repeated cancellation and return the worker outcome."""
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    try:
        return worker.result()
    except BaseException as exc:
        return exc
