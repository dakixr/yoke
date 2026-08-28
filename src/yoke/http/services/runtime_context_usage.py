"""Safe public context-usage projections for HTTP runtime events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from yoke.http.services.event_broker import EventService
from yoke.session import SessionRecord
from yoke.session import SessionStore


_PUBLIC_FIELDS = frozenset(
    {
        "iteration",
        "reason",
        "message_count",
        "input_tokens",
        "total_with_reserve",
        "estimated_input_tokens",
        "estimated_total_with_reserve",
        "accounting_source",
        "provider_reported_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "provider_reported_total_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "max_total_tokens",
        "usage_percent",
    }
)


@dataclass(slots=True)
class RuntimeContextUsageState:
    """Track and persist the newest safe usage snapshot for one runtime turn."""

    max_total_tokens: int | None = None
    latest: dict[str, object] | None = None

    def configure(
        self,
        persisted_context_window: int | None,
        runtime_context_window: object | None,
    ) -> None:
        """Resolve the effective model context limit for this turn."""
        self.max_total_tokens = persisted_context_window
        if isinstance(runtime_context_window, int) and runtime_context_window > 0:
            self.max_total_tokens = runtime_context_window

    def capture(
        self,
        event: str,
        payload: Mapping[str, object],
        *,
        turn_id: int,
        input_id: str,
    ) -> dict[str, object] | None:
        """Capture a safe usage update from an internal agent event."""
        if event == "context_usage":
            usage = public_context_usage(payload)
        elif event == "model_end":
            usage = provider_response_context_usage(
                payload,
                max_total_tokens=self.max_total_tokens,
            )
            if usage is None:
                return None
        else:
            return None
        usage["turnID"] = turn_id
        usage["inputID"] = input_id
        self.latest = dict(usage)
        return usage

    def persist(
        self,
        *,
        store: SessionStore,
        events: EventService,
        session_id: str,
        record: SessionRecord,
    ) -> SessionRecord:
        """Persist and journal the latest usage snapshot, if any."""
        if self.latest is None:
            return record
        updated = store.set_context_usage(
            session_id,
            self.latest,
            existing_record=record,
        )
        events.durable(
            session_id,
            "session.context.updated",
            self.latest,
            location=updated.root,
        )
        return updated


def public_context_usage(payload: Mapping[str, object]) -> dict[str, object]:
    """Allow only internally generated scalar accounting fields."""
    return {
        key: value
        for key, value in payload.items()
        if key in _PUBLIC_FIELDS
        and (value is None or isinstance(value, str | int | float | bool))
    }


def provider_response_context_usage(
    payload: Mapping[str, object],
    *,
    max_total_tokens: int | None,
) -> dict[str, object] | None:
    """Build context usage from an assistant response's provider-reported usage."""
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens")
    if not isinstance(input_tokens, int):
        return None
    if not isinstance(max_total_tokens, int) or max_total_tokens <= 0:
        return None
    result: dict[str, object] = {
        "reason": "provider_response",
        "input_tokens": input_tokens,
        "provider_reported_input_tokens": input_tokens,
        "max_total_tokens": max_total_tokens,
        "usage_percent": min(
            100,
            max(0, round((input_tokens / max_total_tokens) * 100)),
        ),
        "accounting_source": "provider",
    }
    iteration = payload.get("iteration")
    if isinstance(iteration, int):
        result["iteration"] = iteration
    for source, target in (
        ("output_tokens", "output_tokens"),
        ("reasoning_tokens", "reasoning_tokens"),
        ("total_tokens", "provider_reported_total_tokens"),
        ("cached_input_tokens", "cached_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
    ):
        value = usage.get(source)
        if isinstance(value, int):
            result[target] = value
    return result
