"""Thread-safe fan-out broker for one process-wide SSE feed."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
import secrets
from threading import Lock

from yoke.http.models.common import LocationInfo
from yoke.http.models.event import DurableEventInfo
from yoke.http.models.event import PublicEvent
from yoke.session.events import SessionEvent
from yoke.session.events import SessionEventJournal


@dataclass(slots=True)
class EventSubscription:
    """One bounded subscriber queue tied to its owning asyncio loop."""

    id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[PublicEvent | None]
    closed: bool = False


class GlobalEventBroker:
    """Non-blocking event fan-out from worker threads to SSE clients."""

    def __init__(self, *, queue_size: int = 512) -> None:
        self.queue_size = queue_size
        self._lock = Lock()
        self._subscriptions: dict[str, EventSubscription] = {}

    def subscribe(self) -> EventSubscription:
        """Create a bounded subscription on the current asyncio loop."""
        loop = asyncio.get_running_loop()
        subscription = EventSubscription(
            id=f"sub_{secrets.token_hex(8)}",
            loop=loop,
            queue=asyncio.Queue(maxsize=self.queue_size),
        )
        with self._lock:
            self._subscriptions[subscription.id] = subscription
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        """Remove and close one subscription."""
        with self._lock:
            self._subscriptions.pop(subscription.id, None)
        if not subscription.closed:
            subscription.closed = True
            subscription.loop.call_soon_threadsafe(self._close_queue, subscription)

    def close(self) -> None:
        """Close every live subscriber so server shutdown cannot wait on SSE."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
            for subscription in subscriptions:
                subscription.closed = True
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(
                    self._close_queue,
                    subscription,
                )
            except RuntimeError:
                # The owning loop may already be gone during interpreter teardown.
                pass

    def publish(self, event: PublicEvent, *, ephemeral: bool = False) -> None:
        """Fan out one sanitized event without blocking the publishing thread."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            if subscription.closed:
                continue
            subscription.loop.call_soon_threadsafe(
                self._offer,
                subscription,
                event,
                ephemeral,
            )

    def _offer(
        self,
        subscription: EventSubscription,
        event: PublicEvent,
        ephemeral: bool,
    ) -> None:
        if subscription.closed:
            return
        try:
            subscription.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            if ephemeral:
                return
        subscription.closed = True
        while not subscription.queue.empty():
            try:
                subscription.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        resync = live_event(
            "server.resyncRequired",
            {"reason": "slow_consumer"},
        )
        try:
            subscription.queue.put_nowait(resync)
            subscription.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    @staticmethod
    def _close_queue(subscription: EventSubscription) -> None:
        try:
            subscription.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class EventService:
    """Combine durable journal append with live broker publication."""

    def __init__(self, journal: SessionEventJournal, broker: GlobalEventBroker) -> None:
        self.journal = journal
        self.broker = broker

    def durable(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, object] | None = None,
        *,
        location: str | None = None,
    ) -> PublicEvent:
        event = self.journal.append(
            session_id,
            event_type,
            data,
            location=location,
        )
        public = public_event_from_session_event(event)
        self.broker.publish(public)
        return public

    def live(
        self,
        event_type: str,
        data: dict[str, object],
        *,
        session_id: str | None = None,
        location: str | None = None,
    ) -> PublicEvent:
        event = live_event(
            event_type,
            data,
            session_id=session_id,
            location=location,
        )
        self.broker.publish(event, ephemeral=True)
        return event


def public_event_from_session_event(event: SessionEvent) -> PublicEvent:
    """Convert a durable repository event into the stable public envelope."""
    return PublicEvent(
        id=event.id,
        type=event.type,
        time=event.time,
        session_id=event.session_id,
        location=(
            LocationInfo(directory=event.location)
            if event.location is not None
            else None
        ),
        durable=DurableEventInfo(
            aggregate_id=event.session_id,
            seq=event.seq,
            version=event.version,
        ),
        data=event.data,
    )


def live_event(
    event_type: str,
    data: dict[str, object],
    *,
    session_id: str | None = None,
    location: str | None = None,
) -> PublicEvent:
    return PublicEvent(
        id=f"evt_{secrets.token_hex(12)}",
        type=event_type,
        time=datetime.now(UTC).isoformat(),
        session_id=session_id,
        location=LocationInfo(directory=location) if location is not None else None,
        durable=None,
        data=data,
    )
