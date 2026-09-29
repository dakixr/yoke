"""Coalesced invalidations keep chat traffic out of background observers."""

import asyncio
from typing import Any


class Invalidations(asyncio.Queue[dict[str, Any]]):
    """Retain one pending refresh per session for one event type."""

    def __init__(self, event_type: str = "session.process.updated") -> None:
        super().__init__(maxsize=256)
        self.event_type = event_type
        self._queued: set[str] = set()

    def put_nowait(self, item: dict[str, Any]) -> None:
        session_id = item.get("sessionID")
        if item.get("type") != self.event_type or not isinstance(session_id, str):
            return
        if session_id in self._queued:
            return
        super().put_nowait(item)
        self._queued.add(session_id)

    def get_nowait(self) -> dict[str, Any]:
        item = super().get_nowait()
        self._queued.discard(item["sessionID"])
        return item
