"""stdio lifecycle for the Yoke ACP adapter."""

from __future__ import annotations

import asyncio
import signal
import sys

from acp import run_agent

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient


async def _run(url: str, token: str) -> None:
    native = NativeClient(url, token)
    agent = YokeAcpAgent(native)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        await run_agent(agent, use_unstable_protocol=True)
    finally:
        await agent.close()


def run_acp(url: str, token: str) -> int:
    """Run the stdio ACP peer against one authenticated Yoke HTTP daemon."""
    try:
        asyncio.run(_run(url, token))
    except asyncio.CancelledError:
        return 0
    except Exception:  # noqa: BLE001 - never leak credentials or native payloads
        print(
            "Yoke ACP failed; check daemon availability and ACP environment settings.",
            file=sys.stderr,
        )
        return 1
    return 0
