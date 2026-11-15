"""stdio lifecycle for the Yoke ACP adapter."""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import suppress
from collections.abc import Callable

from acp import run_agent

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient

# Daemon clients address the socket; the host part of the URL is never resolved.
DAEMON_SOCKET_URL = "http://yoke"


async def _run(native: NativeClient, *, lease_client: str | None) -> None:
    agent = YokeAcpAgent(native)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except (NotImplementedError, RuntimeError):
                pass
    lease: asyncio.Task[None] | None = None
    if lease_client is not None:
        from yoke.daemon.lease import hold_lease

        lease = asyncio.create_task(hold_lease(lease_client))
    try:
        await run_agent(agent, use_unstable_protocol=True)
    finally:
        try:
            await agent.close()
        finally:
            if lease is not None:
                lease.cancel()
                with suppress(asyncio.CancelledError):
                    await lease


def _serve(
    native_factory: Callable[[], NativeClient], *, lease_client: str | None
) -> int:
    try:
        asyncio.run(_run(native_factory(), lease_client=lease_client))
    except asyncio.CancelledError:
        return 0
    except Exception:  # noqa: BLE001 - never leak credentials or native payloads
        print(
            "Yoke ACP failed; check daemon availability and ACP environment settings.",
            file=sys.stderr,
        )
        return 1
    return 0


def run_acp(url: str, token: str) -> int:
    """Run the stdio ACP peer against an explicit authenticated Yoke HTTP server."""
    return _serve(lambda: NativeClient(url, token), lease_client=None)


def run_acp_with_daemon() -> int:
    """Discover (or start) the per-user daemon, hold a lease, and serve ACP on stdio."""
    from yoke.daemon.client import (
        DaemonIncompatible,
        DaemonUnavailable,
        connect_or_spawn,
    )

    try:
        handle = connect_or_spawn()
    except (DaemonIncompatible, DaemonUnavailable) as exc:
        print(f"Yoke ACP: {exc}", file=sys.stderr)
        return 1
    if handle.warning:
        print(f"Yoke ACP: {handle.warning}", file=sys.stderr)
    socket_path = str(handle.socket)
    return _serve(
        lambda: NativeClient(DAEMON_SOCKET_URL, None, uds=socket_path),
        lease_client="acp",
    )
