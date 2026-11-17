"""Keep a client's lease on the daemon open for as long as the client runs."""

from __future__ import annotations

import asyncio
import logging
import os
import json
from collections.abc import Callable

import httpx

from yoke.daemon.client import DaemonHandle, connect_or_spawn, wait_for_exit
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.transport import async_client

LOGGER = logging.getLogger(__name__)
_RETRY_SECONDS = 0.5


async def hold_lease(
    client: str,
    paths: DaemonPaths | None = None,
    *,
    on_connect: Callable[[DaemonHandle], None] | None = None,
) -> None:
    """Hold ``GET /daemon/lease`` open until cancelled.

    When the stream ends (the daemon restarted for a new build, or crashed),
    rediscover the daemon, starting one if needed, and take a new lease. The
    Unix socket path is stable. ``on_connect`` lets TCP clients refresh their
    port and token before opening other connections.
    """
    paths = paths or DaemonPaths.default()
    params = {"client": client, "pid": str(os.getpid())}
    while True:
        try:
            handle = await asyncio.to_thread(connect_or_spawn, paths)
            async with async_client(
                handle.socket,
                timeout=httpx.Timeout(10.0, read=None),
            ) as http:
                async with http.stream("GET", "daemon/lease", params=params) as stream:
                    stream.raise_for_status()
                    announced = False
                    restarting = False
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if data.get("leaseID") and not announced:
                                announced = True
                                if on_connect is not None:
                                    on_connect(handle)
                            if data.get("reason") == "restart":
                                restarting = True
                            if data.get("reason") == "stop":
                                return
            if restarting:
                # Wait for the old lifetime lock before rediscovery. An immediate
                # probe can otherwise announce the exiting daemon as reconnected.
                await asyncio.to_thread(
                    wait_for_exit, handle.socket, handle.pid, timeout=15, paths=paths
                )
                continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep retrying; the client must not die with the daemon
            LOGGER.debug("Daemon lease interrupted", exc_info=True)
        await asyncio.sleep(_RETRY_SECONDS)
