"""Event-driven uvicorn fixture with complete application lifespan teardown."""

from __future__ import annotations

import asyncio
import socket
from threading import Event

import uvicorn


class FixtureServer(uvicorn.Server):
    """Own a test listener without uvicorn's two fixed shutdown sleeps.

    Tests close their clients and brokers before stopping the listener. Keep
    ASGI lifespan shutdown, which drains runtime resources and storage pools.
    """

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = Event()
        self.error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit = asyncio.Event()

    def run(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            super().run(sockets=sockets)
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    async def main_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.ready.set()
        if not await self.on_tick(0):
            await self._exit.wait()

    def stop(self) -> None:
        self.should_exit = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._exit.set)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        for listener in self.servers:
            listener.close()
        for sock in sockets or []:
            sock.close()
        for connection in list(self.server_state.connections):
            connection.shutdown()
        try:
            tasks = list(self.server_state.tasks)
            if tasks:
                async with asyncio.timeout(1):
                    await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(listener.wait_closed() for listener in self.servers))
        finally:
            await self.lifespan.shutdown()
