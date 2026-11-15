"""The shared ACP event hub delivers every native event in order."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from yoke.acp.observation.hub import NativeEventHub


class _Native:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def events(
        self, queue: asyncio.Queue[dict[str, Any]], ready: asyncio.Event
    ) -> None:
        ready.set()
        for event in self._events:
            queue.put_nowait(event)
        await asyncio.Event().wait()


def test_hub_does_not_coalesce_or_drop_events() -> None:
    sent: list[dict[str, Any]] = [
        {"type": "session.context.updated", "sessionID": "s", "data": {}},
        {"type": "session.plan.updated", "sessionID": "s", "data": {}},
        {"type": "session.question.requested", "sessionID": "s", "data": {}},
        {"type": "catalog.updated", "data": {}},
    ]

    async def scenario() -> list[dict[str, object]]:
        hub = NativeEventHub(cast(Any, _Native(sent)))
        received: list[dict[str, object]] = []
        done = asyncio.Event()

        async def callback(event: dict[str, object] | None) -> None:
            if event is not None:
                received.append(event)
                if len(received) == len(sent):
                    done.set()

        await hub.subscribe(callback)
        await asyncio.wait_for(done.wait(), 5)
        await hub.close()
        return received

    assert asyncio.run(scenario()) == sent
