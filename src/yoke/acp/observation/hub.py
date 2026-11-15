"""One reconnecting native SSE subscription shared by ACP observers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from yoke.acp.observation.events import Invalidations

if TYPE_CHECKING:
    from yoke.acp.native import NativeClient

LOGGER = logging.getLogger(__name__)
EventCallback = Callable[[dict[str, object] | None], Awaitable[None]]


class NativeEventHub:
    """Fan out one native event stream and reconciliation tick to subscribers."""

    def __init__(self, native: NativeClient) -> None:
        self.native = native
        self._callbacks: set[EventCallback] = set()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.ready = asyncio.Event()

    async def subscribe(self, callback: EventCallback) -> None:
        if self._closed:
            return
        self._callbacks.add(callback)
        if self._task is None or self._task.done():
            self.ready = asyncio.Event()
            self._task = asyncio.create_task(self._watch())

    async def unsubscribe(self, callback: EventCallback) -> None:
        self._callbacks.discard(callback)
        if not self._callbacks and self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _watch(self) -> None:
        while self._callbacks and not self._closed:
            queue = Invalidations("*")
            ready = self.ready
            stream = asyncio.create_task(self.native.events(queue, ready))
            tick = asyncio.create_task(asyncio.sleep(15))
            incoming: asyncio.Task[dict[str, object]] | None = None
            try:
                await asyncio.wait_for(ready.wait(), 10)
                if stream.done():
                    await stream
                    raise RuntimeError("Native event stream ended")
                while self._callbacks:
                    incoming = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        (incoming, stream, tick),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stream in done:
                        await stream
                        raise RuntimeError("Native event stream ended")
                    if tick in done:
                        await self._publish(None)
                        tick = asyncio.create_task(asyncio.sleep(15))
                    if incoming in done:
                        await self._publish(incoming.result())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect policy owns stream failures
                LOGGER.warning("Native observation stream disconnected; reconnecting")
                await self._publish({"type": "__disconnected__"})
                await asyncio.sleep(1)
            finally:
                if incoming is not None:
                    incoming.cancel()
                stream.cancel()
                tick.cancel()
                await asyncio.gather(
                    stream,
                    tick,
                    *([incoming] if incoming else []),
                    return_exceptions=True,
                )

    async def _publish(self, event: dict[str, object] | None) -> None:
        for callback in tuple(self._callbacks):
            try:
                await callback(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one observer cannot stop fanout
                LOGGER.exception("ACP observation subscriber failed")

    async def close(self) -> None:
        self._closed = True
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._callbacks.clear()
