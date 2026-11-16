"""Transport-neutral agent events consumed by terminal renderers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from queue import Queue
from threading import Lock
from typing import TypedDict


class BackendEvent(TypedDict):
    """The event type and data from a daemon SSE envelope."""

    type: str
    payload: dict[str, object]


class EventStream:
    """A lossless subscription, closed explicitly by its owner."""

    def __init__(self) -> None:
        self._queue: Queue[BackendEvent | None] = Queue()
        self._lock = Lock()
        self._closed = False

    def publish(self, event: str, payload: dict[str, object]) -> None:
        """Append one agent event without changing its content or ordering."""
        with self._lock:
            if not self._closed:
                self._queue.put({"type": event, "payload": dict(payload)})

    def close(self) -> None:
        """End iteration after every event already published."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)

    def __iter__(self) -> Iterator[BackendEvent]:
        while (event := self._queue.get()) is not None:
            yield event


class EventSubscriptions:
    """Fan out a session's event stream. HTTP equivalent: GET /event?sessionID=."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._streams: set[EventStream] = set()

    def subscribe(self) -> tuple[EventStream, Callable[[], None]]:
        """Open a stream and return its explicit unsubscribe callback."""
        stream = EventStream()
        with self._lock:
            self._streams.add(stream)

        def unsubscribe() -> None:
            with self._lock:
                self._streams.discard(stream)
            stream.close()

        return stream, unsubscribe

    def publish(self, event: str, payload: dict[str, object]) -> None:
        with self._lock:
            for stream in self._streams:
                stream.publish(event, payload)

    def close(self) -> None:
        """Close every subscription when its session connection closes."""
        with self._lock:
            for stream in self._streams:
                stream.close()
            self._streams.clear()


# These names match the daemon publisher, including the Phase 4 CLI events.
PUBLIC_AGENT_EVENTS = {
    "model_start": "session.model.started",
    "model_end": "session.model.ended",
    "provider_retry": "session.provider.retry",
    "provider_rate_limited": "session.provider.rate_limited",
    "provider_recovered": "session.provider.recovered",
    "notice": "session.notice",
    "warning": "session.warning",
    "error": "session.error",
    "compaction_summary_start": "session.compaction.summary.started",
    "compaction_summary_end": "session.compaction.summary.ended",
    "context_compaction": "session.context.compacted",
    "assistant_message": "session.message.updated",
    "tool_execution_start": "session.tool.started",
    "tool_execution_end": "session.tool.ended",
    "tool_execution_output_delta": "session.tool.delta",
    "compaction_start": "session.compaction.started",
    "compaction_progress": "session.compaction.delta",
    "compaction_end": "session.compaction.ended",
    "context_usage": "session.context.updated",
}
AGENT_EVENTS = {public: agent for agent, public in PUBLIC_AGENT_EVENTS.items()}


def agent_event(event: BackendEvent) -> tuple[str, dict[str, object]]:
    """Project a transport event onto the existing terminal rendering vocabulary."""
    return AGENT_EVENTS.get(event["type"], event["type"]), event["payload"]


def event_from_sse(envelope: Mapping[str, object]) -> BackendEvent:
    """Decode an SSE PublicEvent's type and data into the terminal stream."""
    event_type, data = envelope.get("type"), envelope.get("data")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        raise ValueError("Expected an SSE event type and data object.")
    payload: dict[str, object] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError("Expected string event payload keys.")
        payload[key] = value
    return {"type": event_type, "payload": payload}
