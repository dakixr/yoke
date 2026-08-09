"""Structured, human-readable observation for SDK agent runs."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import sys
import threading
from typing import cast, Literal, Protocol, TextIO

from yoke.agent.loop.types import AgentEventHandler

type TraceDetail = Literal["quiet", "messages", "actions", "full"]

_DETAILS = {"quiet", "messages", "actions", "full"}
_MESSAGE_EVENTS = {"assistant_message", "agent_end", "agent_error"}
_ACTION_EVENTS = {
    *_MESSAGE_EVENTS,
    "batch_attempt_error",
    "tool_execution_start",
    "tool_execution_end",
}
_SENSITIVE_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "privatekey",
    "secret",
)
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class AgentTraceEvent:
    """One structured event from an SDK agent run."""

    name: str
    payload: Mapping[str, object]
    task_id: str | None = None
    attempt: int | None = None
    timestamp: datetime | None = None


class AgentObserver(Protocol):
    """Receive structured events from SDK agent runs."""

    def observe(self, event: AgentTraceEvent) -> None:
        """Observe one agent event."""
        ...


class CompositeObserver:
    """Send each event to multiple observers in order."""

    def __init__(self, *observers: AgentObserver) -> None:
        self.observers = observers

    def observe(self, event: AgentTraceEvent) -> None:
        """Forward one event to each observer."""
        for observer in self.observers:
            _observe_safely(observer, event)


class ConsoleObserver:
    """Write compact live agent activity to a text stream."""

    def __init__(
        self,
        detail: TraceDetail = "actions",
        *,
        stream: TextIO | None = None,
        label: str | None = None,
        max_length: int = 1000,
    ) -> None:
        self.detail: TraceDetail = _validate_detail(detail)
        self.stream = stream or sys.stdout
        self.label = label
        self.max_length = max_length
        self._lock = threading.Lock()

    def observe(self, event: AgentTraceEvent) -> None:
        """Render and write one accepted event."""
        rendered = render_trace_event(
            event,
            detail=self.detail,
            label=self.label,
            max_length=self.max_length,
        )
        if rendered is None:
            return
        with self._lock:
            print(rendered, file=self.stream, flush=True)


class LoggingObserver:
    """Send compact agent activity to a Python logger."""

    def __init__(
        self,
        logger: logging.Logger,
        detail: TraceDetail = "actions",
        *,
        level: int = logging.INFO,
        label: str | None = None,
        max_length: int = 1000,
    ) -> None:
        self.logger = logger
        self.detail: TraceDetail = _validate_detail(detail)
        self.level = level
        self.label = label
        self.max_length = max_length

    def observe(self, event: AgentTraceEvent) -> None:
        """Render and log one accepted event."""
        rendered = render_trace_event(
            event,
            detail=self.detail,
            label=self.label,
            max_length=self.max_length,
        )
        if rendered is not None:
            self.logger.log(self.level, rendered)


class JsonlObserver:
    """Append filtered structured agent events to a JSON Lines file."""

    def __init__(
        self,
        path: str | Path,
        detail: TraceDetail = "full",
        *,
        label: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.detail: TraceDetail = _validate_detail(detail)
        self.label = label
        self._lock = threading.Lock()

    def observe(self, event: AgentTraceEvent) -> None:
        """Serialize and append one accepted event."""
        if not trace_event_is_visible(event, self.detail):
            return
        payload = {
            "timestamp": (event.timestamp or datetime.now(UTC)).isoformat(),
            "event": event.name,
            "task_id": event.task_id,
            "attempt": event.attempt,
            "label": self.label,
            "payload": _redact(dict(event.payload)),
        }
        line = json.dumps(payload, default=str, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")


@dataclass(slots=True, frozen=True)
class BoundObserver:
    """Add batch task identity to events sent to another observer."""

    observer: AgentObserver
    task_id: str
    attempt: int

    def observe(self, event: AgentTraceEvent) -> None:
        """Forward one event with this batch attempt's identity."""
        _observe_safely(
            self.observer,
            AgentTraceEvent(
                name=event.name,
                payload=event.payload,
                task_id=self.task_id,
                attempt=self.attempt,
                timestamp=event.timestamp,
            ),
        )


def compose_event_handler(
    on_event: AgentEventHandler | None,
    observers: tuple[AgentObserver | None, ...],
) -> AgentEventHandler | None:
    """Combine the legacy callback and SDK observers into one handler."""
    active = tuple(observer for observer in observers if observer is not None)
    if on_event is None and not active:
        return None

    def handle(name: str, payload: dict[str, object]) -> None:
        if on_event is not None:
            on_event(name, payload)
        notify_observers(active, name, payload)

    return handle


def notify_observers(
    observers: tuple[AgentObserver, ...],
    name: str,
    payload: Mapping[str, object],
) -> None:
    """Send one immutable event view to each observer once."""
    event = AgentTraceEvent(
        name=name,
        payload=dict(payload),
        timestamp=datetime.now(UTC),
    )
    seen: set[int] = set()
    for observer in observers:
        identity = id(observer)
        if identity not in seen:
            _observe_safely(observer, event)
            seen.add(identity)


def _observe_safely(observer: AgentObserver, event: AgentTraceEvent) -> None:
    try:
        observer.observe(_snapshot_event(event))
    except Exception:
        LOGGER.exception("Agent observer failed while handling %s", event.name)


def _snapshot_event(event: AgentTraceEvent) -> AgentTraceEvent:
    return AgentTraceEvent(
        name=event.name,
        payload=deepcopy(dict(event.payload)),
        task_id=event.task_id,
        attempt=event.attempt,
        timestamp=event.timestamp,
    )


def render_trace_event(
    event: AgentTraceEvent,
    *,
    detail: TraceDetail = "actions",
    label: str | None = None,
    max_length: int = 1000,
) -> str | None:
    """Render one event as compact plain text."""
    _validate_detail(detail)
    if not trace_event_is_visible(event, detail):
        return None
    prefix = _event_prefix(event, label)
    payload = event.payload
    if event.name == "assistant_message":
        text = payload.get("content")
        return _truncate(_prefixed(prefix, str(text)), max_length) if text else None
    if event.name == "agent_end":
        text = payload.get("output")
        return (
            _truncate(_prefixed(prefix, str(text)), max_length)
            if text
            else f"{prefix}✓ completed"
        )
    if event.name == "agent_error":
        return _truncate(
            f"{prefix}! {payload.get('error', 'agent failed')}", max_length
        )
    if event.name == "batch_attempt_error":
        stage = payload.get("stage", "batch attempt")
        error = payload.get("error", "failed")
        return _truncate(f"{prefix}! {stage}: {error}", max_length)
    if event.name == "tool_execution_start":
        tool = payload.get("tool_name", "tool")
        arguments = _format_arguments(payload.get("tool_arguments"))
        suffix = f"({arguments})" if arguments else "()"
        return _truncate(f"{prefix}→ {tool}{suffix}", max_length)
    if event.name == "tool_execution_end" and not payload.get("ok", False):
        tool = payload.get("tool_name", "tool")
        error = _tool_error(payload)
        return _truncate(f"{prefix}← {tool} failed: {error}", max_length)
    compact = json.dumps(_redact(dict(payload)), default=str, ensure_ascii=False)
    return _truncate(f"{prefix}· {event.name} {compact}", max_length)


def trace_event_is_visible(event: AgentTraceEvent, detail: TraceDetail) -> bool:
    """Return whether a detail level includes an event."""
    if detail == "quiet":
        return False
    if detail == "messages":
        return event.name in _MESSAGE_EVENTS
    if detail == "actions":
        return event.name in _ACTION_EVENTS and (
            event.name != "tool_execution_end" or not event.payload.get("ok", False)
        )
    return True


def _event_prefix(event: AgentTraceEvent, label: str | None) -> str:
    identity = event.task_id or label
    if identity is None:
        return ""
    attempt = f"#{event.attempt}" if event.attempt and event.attempt > 1 else ""
    return f"[{identity}{attempt}] "


def _prefixed(prefix: str, text: str) -> str:
    lines = text.strip().splitlines() or [""]
    return "\n".join(f"{prefix}{line}" for line in lines)


def _format_arguments(raw: object) -> str:
    arguments = _parse_arguments(raw)
    if arguments is None:
        return "<unavailable>" if raw else ""
    redacted = cast(dict[str, object], _redact(arguments))
    parts = [f"{key}={_compact(value, 72)}" for key, value in redacted.items()]
    return _truncate(", ".join(parts), 220)


def _parse_arguments(raw: object) -> dict[str, object] | None:
    if isinstance(raw, dict) and all(isinstance(key, str) for key in raw):
        return cast(dict[str, object], raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and all(isinstance(key, str) for key in parsed):
        return parsed
    return None


def _redact(value: object, key: str = "") -> object:
    normalized_key = key.casefold().replace("-", "_")
    is_token = normalized_key == "token" or (
        normalized_key.endswith("_token") and not normalized_key.endswith("_tokens")
    )
    if is_token or any(part in normalized_key for part in _SENSITIVE_PARTS):
        return "<redacted>"
    if isinstance(value, str) and normalized_key in {
        "tool_arguments",
        "executed_arguments",
    }:
        parsed = _parse_arguments(value)
        return _redact(parsed) if parsed is not None else "<unavailable>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _compact(value: object, limit: int) -> str:
    text = json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False)
    return _truncate(" ".join(text.split()), limit)


def _tool_error(payload: Mapping[str, object]) -> str:
    result = payload.get("result")
    if isinstance(result, Mapping):
        error = cast(Mapping[str, object], result).get("error")
        if error:
            return str(error)
    return "tool returned an error"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _validate_detail(detail: TraceDetail) -> TraceDetail:
    if detail not in _DETAILS:
        choices = ", ".join(sorted(_DETAILS))
        raise ValueError(f"detail must be one of: {choices}")
    return detail
