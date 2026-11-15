"""Keep a client's lease on the daemon open for as long as the client runs."""

from __future__ import annotations

import asyncio
import logging
import os
import json

import httpx

from yoke.daemon.client import connect_or_spawn
from yoke.daemon.paths import DaemonPaths

LOGGER = logging.getLogger(__name__)
_RETRY_SECONDS = 0.5


async def hold_lease(client: str, paths: DaemonPaths | None = None) -> None:
    """Hold ``GET /daemon/lease`` open until cancelled.

    When the stream ends (the daemon restarted for a new build, or crashed),
    rediscover the daemon, starting one if needed, and take a new lease. The
    socket path is stable, so the client's other connections keep working.
    """
    paths = paths or DaemonPaths.default()
    params = {"client": client, "pid": str(os.getpid())}
    while True:
        try:
            handle = await asyncio.to_thread(connect_or_spawn, paths)
            transport = httpx.AsyncHTTPTransport(uds=str(handle.socket))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://yoke/api/v1/",
                timeout=httpx.Timeout(10.0, read=None),
                trust_env=False,
            ) as http:
                async with http.stream("GET", "daemon/lease", params=params) as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if data.get("reason") == "stop":
                                return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep retrying; the client must not die with the daemon
            LOGGER.debug("Daemon lease interrupted", exc_info=True)
        await asyncio.sleep(_RETRY_SECONDS)
