"""Session-wide agent observation survives foreground turn settlement."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import TYPE_CHECKING

from yoke.acp.observation.agents import AgentRoster, Update
from yoke.acp.observation.errors import NativeHttpError
from yoke.acp.observation.events import Invalidations

if TYPE_CHECKING:
    from yoke.acp.native import NativeClient

LOGGER = logging.getLogger(__name__)


class AgentMonitor:
    """One reconnecting native stream for all attached ACP sessions."""

    def __init__(
        self,
        native: NativeClient,
        *,
        reconciliation_tick: Callable[[], Awaitable[None]] | None = None,
        reconnect_tick: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.native = native
        self._lock = asyncio.Lock()
        self._sessions: dict[str, AgentRoster] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._closed = False
        self._unsupported = False
        self._capability_checked = False
        self._tick = reconciliation_tick or self._reconciliation_tick
        self._retry = reconnect_tick or self._reconnect_tick

    @staticmethod
    async def _reconciliation_tick() -> None:
        await asyncio.sleep(15)

    @staticmethod
    async def _reconnect_tick() -> None:
        await asyncio.sleep(1)

    async def attach(self, session_id: str, update: Update) -> None:
        """Replay an initial roster without requiring a prompt or process tool."""
        async with self._lock:
            if self._closed or self._unsupported:
                return
            if not self._capability_checked:
                try:
                    async with asyncio.timeout(10):
                        spec = await self.native.request("GET", "openapi.json")
                    self._unsupported = "get" not in spec.get("paths", {}).get(
                        "/api/v1/agent-run", {}
                    )
                    self._capability_checked = True
                except Exception:  # noqa: BLE001 - try the endpoint if discovery fails
                    pass
                if self._unsupported:
                    return
            self._sessions[session_id] = AgentRoster(session_id, update)
            try:
                await self._refresh(session_id)
            except Exception:  # noqa: BLE001 - observation cannot break session load
                LOGGER.warning("Initial agent observation unavailable")
            if self._unsupported:
                self._sessions.clear()
            elif self._sessions and (
                self._watch_task is None or self._watch_task.done()
            ):
                self._watch_task = asyncio.create_task(self._watch())

    async def _refresh(self, session_id: str) -> None:
        try:
            async with asyncio.timeout(10):
                items = await self.native.data(
                    "GET", "agent-run", params={"sessionID": session_id}
                )
        except NativeHttpError as exc:
            if exc.status == 404 and self._capability_checked:
                roster = self._sessions.pop(session_id, None)
                if roster is not None:
                    await roster.lost()
                return
            if exc.status in (404, 405, 501):
                self._unsupported = True
                return
            raise
        await self._sessions[session_id].reconcile(items)
        # A successful endpoint read establishes support even if the earlier
        # OpenAPI request failed. A later session 404 is not a daemon downgrade.
        self._capability_checked = True

    async def _watch(self) -> None:
        while not self._closed and not self._unsupported and self._sessions:
            queue = Invalidations("session.agent.updated")
            ready = asyncio.Event()
            stream = asyncio.create_task(self.native.events(queue, ready))
            tick = asyncio.ensure_future(self._tick())
            try:
                await asyncio.wait_for(ready.wait(), 10)
                if stream.done():
                    await stream
                    raise RuntimeError("Agent event stream ended")
                # This second read closes the initial snapshot/subscribe race,
                # and every reconnect starts with an authoritative roster.
                async with self._lock:
                    for session_id in list(self._sessions):
                        await self._refresh(session_id)
                while not self._unsupported and self._sessions:
                    incoming = asyncio.create_task(queue.get())
                    try:
                        done, _ = await asyncio.wait(
                            (incoming, stream, tick),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stream in done:
                            await stream
                            raise RuntimeError("Agent event stream ended")
                        refresh_all = tick in done
                        if refresh_all:
                            await tick
                            tick = asyncio.ensure_future(self._tick())
                        owner = (
                            incoming.result()["sessionID"] if incoming in done else None
                        )
                        async with self._lock:
                            for session_id in list(self._sessions):
                                if refresh_all or session_id == owner:
                                    await self._refresh(session_id)
                    finally:
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect instead of claiming job failure
                LOGGER.warning("Agent observation disconnected; will reconcile")
                async with self._lock:
                    await self._lost()
            finally:
                stream.cancel()
                tick.cancel()
                await asyncio.gather(stream, tick, return_exceptions=True)
            if self._unsupported:
                async with self._lock:
                    await self._lost()
                    self._sessions.clear()
            if not self._unsupported:
                await self._retry()

    async def _lost(self) -> None:
        for roster in self._sessions.values():
            try:
                async with asyncio.timeout(5):
                    await roster.lost()
            except Exception:  # noqa: BLE001 - release other observers after peer failure
                LOGGER.warning("Could not publish lost agent observation")

    async def detach(self, session_id: str) -> None:
        """Release one session's observation without signalling any process."""
        async with self._lock:
            roster = self._sessions.pop(session_id, None)
            if roster is not None:
                try:
                    async with asyncio.timeout(5):
                        await roster.lost()
                except Exception:  # noqa: BLE001 - a closed ACP peer cannot receive updates
                    LOGGER.warning("Could not close agent observation")
            watch = self._watch_task if not self._sessions else None
            if watch is not None:
                watch.cancel()
                self._watch_task = None
        if watch is not None:
            await asyncio.gather(watch, return_exceptions=True)

    async def close(self) -> None:
        """Close every watcher, leaving native jobs untouched."""
        self._closed = True
        watch = self._watch_task
        if watch is not None:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        async with self._lock:
            await self._lost()
            self._sessions.clear()
            self._watch_task = None
