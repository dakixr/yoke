"""Internal asyncio bridges for the synchronous SDK runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import threading


async def run_sync_cooperatively[ResultT](
    call: Callable[[], ResultT],
    *,
    timeout: float | None,
    stop_event: threading.Event,
) -> ResultT:
    """Run sync work and cooperatively stop it on async interruption."""
    worker = asyncio.create_task(asyncio.to_thread(call))
    try:
        if timeout is None:
            return await asyncio.shield(worker)
        return await asyncio.wait_for(asyncio.shield(worker), timeout)
    except TimeoutError:
        stop_event.set()
        observe_worker(worker)
        raise
    except asyncio.CancelledError:
        stop_event.set()
        observe_worker(worker)
        raise


def observe_worker[ResultT](worker: asyncio.Task[ResultT]) -> None:
    """Detach a sync worker while consuming its eventual outcome."""
    worker.add_done_callback(_consume_worker_outcome)


def _consume_worker_outcome(worker: asyncio.Task[object]) -> None:
    """Retrieve a detached worker's result so failures are not leaked."""
    try:
        worker.result()
    except BaseException:
        pass


async def drain_worker[ResultT](
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
